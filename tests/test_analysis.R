#!/usr/bin/env Rscript
# Exercise valid roster identifiers through the same CLI used for real runs.

check_roster_ids = function() {
  run = tempfile("dd-bs-analysis-")
  dir.create(run)
  on.exit(unlink(run, recursive = TRUE), add = TRUE)

  units = data.frame(
    subject_id = c("deparse.level", "make.row.names", "stringsAsFactors",
                   "factor.exclude", "001", "1"),
    block_id = rep(c("01", "1", "02"), each = 2),
    block_size = rep(c("04096", "8192"), 3),
    position_in_block = rep(1:2, 3)
  )
  plan = units[rep(seq_len(nrow(units)), each = 2), ]
  plan$run_id = "run-fixture"
  plan$planned_order = seq_len(nrow(plan))
  plan$trial_id = paste0("trial-", plan$planned_order)
  plan$trial_in_subject = rep(1:2, nrow(units))
  plan$planned_trials_per_subject = 2
  plan$bytes_requested = 2^20
  plan$block_size_bytes = as.integer(plan$block_size)
  trials = transform(plan, actual_order = planned_order, status = "ok",
                     wall_seconds = planned_order,
                     bytes_written = bytes_requested)
  write.csv(plan, file.path(run, "plan.csv"), row.names = FALSE)
  write.csv(trials, file.path(run, "trials.csv"), row.names = FALSE)

  result = system2(file.path(R.home("bin"), "Rscript"),
                   c("analyze.R", shQuote(run), "--permutations", "19"))
  stopifnot(result == 0L)
  output = list.files(file.path(run, "analysis"), "^fit-", full.names = TRUE)
  stopifnot(length(output) == 1L)
  labels = c(subject_id = "character", block_id = "character",
             block_size = "character")
  observed = read.csv(file.path(output, "subjects.csv"), colClasses = labels)
  stopifnot(identical(observed$subject_id, units$subject_id),
            identical(observed$block_id, units$block_id),
            identical(observed$block_size, units$block_size),
            all(observed$completed_trials == 2L),
            all(observed$included_in_primary))
}

check_roster_ids()
