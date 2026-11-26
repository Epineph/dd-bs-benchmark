#!/usr/bin/env Rscript
# Analyze randomized complete blocks using the assignment unit.

usage <- function() {
  cat(paste0(
    "Usage: Rscript analyze.R RUN_DIR [--reference SIZE] ",
    "[--permutations N]\n",
    "                         [--seed N] [--covariates NAME,NAME]\n",
    "Reads plan.csv and trials.csv. Writes a new folder under analysis/.\n"
  ))
}

parse_args <- function(args) {
  if (length(args) == 0L || any(args %in% c("-h", "--help"))) {
    usage()
    quit(status = if (length(args) == 0L) 2L else 0L)
  }
  opts <- list(run_dir = args[1L], reference = NULL, permutations = 9999L,
               seed = 1L, covariates = character())
  remaining <- args[-1L]
  if (length(remaining) %% 2L != 0L) {
    stop("Each option requires a value.", call. = FALSE)
  }
  allowed <- c("--reference", "--permutations", "--seed", "--covariates")
  for (i in seq_along(remaining)[seq_along(remaining) %% 2L == 1L]) {
    flag <- remaining[i]
    value <- remaining[i + 1L]
    if (!flag %in% allowed) {
      stop(sprintf("Unknown option %s", flag), call. = FALSE)
    }
    key <- substring(flag, 3L)
    if (key %in% c("permutations", "seed")) {
      count <- suppressWarnings(as.integer(value))
      minimum <- if (key == "seed") 0L else 1L
      if (!grepl("^[0-9]+$", value) || is.na(count) || count < minimum) {
        stop(sprintf("Invalid %s value", flag), call. = FALSE)
      }
      value <- count
    }
    if (key == "covariates") {
      value <- unique(trimws(strsplit(value, ",", fixed = TRUE)[[1L]]))
    }
    opts[[key]] <- value
  }
  opts
}

read_benchmark_csv <- function(path) {
  read.csv(
    path, colClasses = c(run_id = "character", trial_id = "character",
                         block_id = "character", subject_id = "character",
                         block_size = "character"),
    na.strings = "", check.names = FALSE, fileEncoding = "UTF-8"
  )
}

write_result <- function(table, directory, filename) {
  write.csv(table, file.path(directory, filename),
            row.names = FALSE, na = "", fileEncoding = "UTF-8")
}


# -- Trial and subject data ----------------------------------------------------

prepare_units <- function(plan, trials, covariates) {
  assignment_keys <- c("run_id", "trial_id", "subject_id", "block_id",
                       "block_size", "position_in_block", "planned_order",
                       "trial_in_subject", "bytes_requested", "block_size_bytes")
  if (!nrow(plan) || anyNA(plan[assignment_keys]) ||
      length(unique(plan$run_id)) != 1L || anyDuplicated(plan$trial_id) ||
      anyDuplicated(trials$trial_id) ||
      !identical(plan$planned_order, seq_len(nrow(plan)))) {
    stop("The planned schedule has missing or duplicate identifiers or order.",
         call. = FALSE)
  }
  known <- match(trials$trial_id, plan$trial_id)
  if (anyNA(known) || nrow(trials) && (
    anyNA(trials[assignment_keys]) ||
    !all(trials[assignment_keys] == plan[known, assignment_keys]) ||
    !identical(trials$actual_order, plan$planned_order[known])
  )) {
    stop("Recorded trial assignments or order disagree with plan.csv.",
         call. = FALSE)
  }

  # Timing and bytes are authoritative. Preserve failed assignments as rows.
  match_trial <- match(plan$trial_id, trials$trial_id)
  elapsed <- trials$wall_seconds[match_trial]
  bytes <- trials$bytes_written[match_trial]
  status <- trials$status[match_trial]
  valid <- which(status == "ok" & is.finite(elapsed) & elapsed > 0 &
                 is.finite(bytes) & bytes == plan$bytes_requested)
  log_rate <- rep(NA_real_, nrow(plan))
  log_rate[valid] <- log(bytes[valid] / 2^20 / elapsed[valid])

  allowed <- c(grep("^pre_", names(trials), value = TRUE),
               "device_payload_mib_before", "position_in_block")
  if (any(!covariates %in% allowed)) {
    unknown <- covariates[!covariates %in% allowed]
    stop(sprintf("Use pre-trial covariates. Unknown names: %s",
                 paste(unknown, collapse = ", ")), call. = FALSE)
  }

  subject_order <- unique(plan$subject_id)
  groups <- split(seq_len(nrow(plan)),
                  factor(plan$subject_id, levels = subject_order))
  rows <- lapply(groups, function(index) {
    first <- index[1L]
    assignment <- plan[first, , drop = FALSE]
    if (length(unique(plan$block_id[index])) != 1L ||
        length(unique(plan$block_size[index])) != 1L ||
        length(index) != assignment$planned_trials_per_subject) {
      stop("An assignment unit spans multiple conditions or has missing trials.",
           call. = FALSE)
    }
    complete <- all(is.finite(log_rate[index]))
    unit <- data.frame(
      block_id = assignment$block_id, subject_id = assignment$subject_id,
      block_size = assignment$block_size, position_in_block =
        assignment$position_in_block,
      expected_trials = length(index), completed_trials =
        sum(is.finite(log_rate[index])),
      complete = complete,
      median_trial_seconds =
        if (complete) median(elapsed[index]) else NA_real_,
      mean_log_mib_s = if (complete) mean(log_rate[index]) else NA_real_
    )
    for (column in setdiff(covariates, "position_in_block")) {
      unit[[column]] <- trials[[column]][match_trial[first]]
    }
    unit
  })
  # Subject IDs are data. A name such as "deparse.level" is an rbind argument.
  units = do.call(rbind, unname(rows))
  units$geomean_mib_s <- exp(units$mean_log_mib_s)
  rownames(units) <- NULL
  units
}

complete_blocks <- function(units, conditions) {
  groups <- split(seq_len(nrow(units)), units$block_id)
  names(Filter(function(index) {
    length(index) == length(conditions) &&
      setequal(units$block_size[index], conditions) &&
      all(units$complete[index])
  }, groups))
}

outcome_matrix <- function(units, blocks, conditions) {
  y <- matrix(NA_real_, nrow = length(blocks), ncol = length(conditions),
              dimnames = list(blocks, conditions))
  selected <- units[units$block_id %in% blocks, , drop = FALSE]
  y[cbind(match(selected$block_id, blocks),
          match(selected$block_size, conditions))] <- selected$mean_log_mib_s
  y
}


# -- Design-based tests --------------------------------------------------------

permutation_test <- function(y, draws) {
  statistic <- function(value) sum((colMeans(value) - mean(value))^2)
  observed <- statistic(y)
  tolerance <- 64 * .Machine$double.eps * max(1, observed)
  exceed <- 0
  shuffled <- y
  for (unused in seq_len(draws)) {
    for (block in seq_len(nrow(y))) {
      shuffled[block, ] <- y[block, sample.int(ncol(y))]
    }
    exceed <- exceed + (statistic(shuffled) >= observed - tolerance)
  }
  (1 + exceed) / (1 + draws)
}

pair_test <- function(differences, draws) {
  observed <- abs(mean(differences))
  tolerance <- 64 * .Machine$double.eps * max(1, observed)
  n <- length(differences)
  if (n <= 16L) {
    sums <- 0
    for (value in differences) {
      sums <- c(sums + value, sums - value)
    }
    p <- mean(abs(sums) / n >= observed - tolerance)
    return(list(p = p, method = "exact_block_sign_flip", draws = length(sums)))
  }

  exceed <- 0
  for (unused in seq_len(draws)) {
    signs <- 2L * sample.int(2L, n, replace = TRUE) - 3L
    permuted <- abs(mean(differences * signs))
    exceed <- exceed + (permuted >= observed - tolerance)
  }
  list(p = (1 + exceed) / (1 + draws),
       method = "monte_carlo_block_sign_flip", draws = draws)
}

run_tests <- function(y, reference, opts) {
  alternatives <- setdiff(colnames(y), reference)
  n <- nrow(y)
  tested <- n >= 2L
  set.seed(opts$seed)
  global <- if (tested) permutation_test(y, opts$permutations) else NA_real_
  tests <- data.frame(
    test = "global", block_size = NA_character_, reference = NA_character_,
    blocks = n, ratio_to_reference = NA_real_, p_value = global,
    p_holm = NA_real_, method =
      if (tested) "monte_carlo_block_permutation" else "insufficient_blocks",
    draws = if (tested) opts$permutations else NA_integer_, seed = opts$seed
  )
  pair_rows <- lapply(alternatives, function(condition) {
    difference <- y[, condition] - y[, reference]
    fit <- if (tested) pair_test(difference, opts$permutations) else {
      list(p = NA_real_, method = "insufficient_blocks", draws = NA_integer_)
    }
    data.frame(
      test = "vs_reference", block_size = condition, reference = reference,
      blocks = n, ratio_to_reference =
        if (n) exp(mean(difference)) else NA_real_,
      p_value = fit$p, p_holm = NA_real_, method = fit$method,
      draws = fit$draws, seed = opts$seed
    )
  })
  pairs <- do.call(rbind, pair_rows)
  pairs$p_holm <- p.adjust(pairs$p_value, method = "holm")
  rbind(tests, pairs)
}


# -- Optional baseline-covariate sensitivity ----------------------------------

fit_covariates <- function(units, conditions, reference, covariates, output) {
  for (name in covariates) {
    if (!is.numeric(units[[name]]) && !all(is.na(units[[name]]))) {
      stop(sprintf("Covariate %s must be numeric.", name), call. = FALSE)
    }
    units$complete <- units$complete & is.finite(units[[name]])
  }
  eligible <- complete_blocks(units, conditions)
  model_data <- units[units$block_id %in% eligible, , drop = FALSE]
  model <- data.frame(
    status = "insufficient_blocks", complete_blocks = length(eligible),
    complete_subjects = nrow(model_data), covariates =
      paste(covariates, collapse = ","),
    reference = reference, residual_df = NA_integer_
  )
  coefficients <- data.frame(
    term = character(), log_estimate = numeric(), standard_error = numeric()
  )
  if (length(eligible) >= 2L) {
    model_data$block_id <- factor(model_data$block_id)
    model_data$block_size <- factor(
      model_data$block_size,
      levels = c(reference, setdiff(conditions, reference))
    )
    formula <- reformulate(c("block_id", "block_size", covariates),
                           response = "mean_log_mib_s")
    fit <- lm(formula, data = model_data)
    model$residual_df <- df.residual(fit)
    if (fit$rank < length(coef(fit))) {
      model$status <- "rank_deficient"
    } else if (model$residual_df <= 0) {
      model$status <- "insufficient_residual_df"
    } else {
      model$status <- "fit"
      estimates <- coef(summary(fit))
      coefficients <- data.frame(term = rownames(estimates),
                                 log_estimate = estimates[, "Estimate"],
                                 standard_error = estimates[, "Std. Error"])
    }
  }
  write_result(model, output, "covariate_model.csv")
  write_result(coefficients, output, "covariate_coefficients.csv")
}


# -- Entrypoint ---------------------------------------------------------------

main <- function() {
  opts <- parse_args(commandArgs(trailingOnly = TRUE))
  plan <- read_benchmark_csv(file.path(opts$run_dir, "plan.csv"))
  trials <- read_benchmark_csv(file.path(opts$run_dir, "trials.csv"))
  units <- prepare_units(plan, trials, opts$covariates)
  sizes <- unique(plan[c("block_size", "block_size_bytes")])
  sizes <- sizes[order(sizes$block_size_bytes), , drop = FALSE]
  conditions <- sizes$block_size
  reference <- opts$reference
  if (is.null(reference)) {
    reference <- if ("4M" %in% conditions) "4M" else conditions[1L]
  }
  if (!reference %in% conditions) {
    stop(sprintf("Unknown reference block size %s", reference), call. = FALSE)
  }
  blocks <- complete_blocks(units, conditions)
  units$included_in_primary <- units$block_id %in% blocks
  y <- outcome_matrix(units, blocks, conditions)

  by_condition <- function(values) {
    as.integer(table(factor(values, levels = conditions)))
  }
  completed <- rep(units$block_size, units$completed_trials)
  speed <- if (length(blocks)) exp(colMeans(y)) else rep(NA_real_, ncol(y))
  median_seconds <- vapply(conditions, function(condition) {
    included <- units$included_in_primary & units$block_size == condition
    median(units$median_trial_seconds[included])
  }, numeric(1))
  best_time_multiplier <- if (length(blocks)) {
    median_seconds / min(median_seconds)
  } else {
    median_seconds
  }
  near_fastest <- which(best_time_multiplier <= 1.05)
  choice <- if (length(near_fastest)) near_fastest[1L] else NA_integer_
  summary <- data.frame(
    sizes, planned_trials = by_condition(plan$block_size),
    completed_trials = by_condition(completed),
    randomized_subjects = by_condition(units$block_size),
    included_subjects =
      by_condition(units$block_size[units$included_in_primary]),
    complete_blocks = length(blocks),
    geomean_mib_s = unname(speed),
    median_trial_seconds = unname(median_seconds),
    time_multiplier_vs_fastest = unname(best_time_multiplier),
    smallest_within_5pct_of_fastest_time =
      if (is.na(choice)) NA else seq_along(conditions) == choice
  )

  parent <- file.path(opts$run_dir, "analysis")
  dir.create(parent, showWarnings = FALSE, recursive = TRUE)
  staging <- tempfile(pattern = ".writing-", tmpdir = parent)
  if (!dir.create(staging)) {
    stop("Could not create the analysis directory.", call. = FALSE)
  }
  on.exit(unlink(staging, recursive = TRUE), add = TRUE)
  write_result(units, staging, "subjects.csv")
  write_result(summary, staging, "conditions.csv")
  write_result(run_tests(y, reference, opts), staging, "randomization.csv")
  if (length(opts$covariates)) {
    fit_covariates(units, conditions, reference, opts$covariates, staging)
  }
  output <- file.path(parent, sub("^\\.writing-", "fit-", basename(staging)))
  if (!file.rename(staging, output)) {
    stop("Could not publish the analysis directory.", call. = FALSE)
  }

  cat(sprintf("Complete blocks: %d/%d\n", length(blocks),
              length(unique(units$block_id))))
  if (length(blocks) < 2L) {
    cat("With fewer than two complete blocks, results are descriptive.\n")
  }
  if (!is.na(choice)) {
    cat(sprintf("Smallest within 5%% of the fastest median time: %s\n",
                conditions[choice]))
  }
  cat(sprintf("Analysis: %s\n", output))
}

main()
