# Changelog

## 0.4.0 (2026-09-27)

- `rescale` / `Pst.rescale_par()`: move parameter values into `SCALE` so each selected parameter
  starts at 1 (bounds divided by the value), or back with `--undo`; the model sees the same values.
  Selection by `--par` globs and `--pargp` (fixed parameters included unless `--exclude_fixed`;
  tied children follow their parent). Prior-information equations are rewritten exactly (every
  residual is unchanged), and settings in parameter units that cannot be converted (absolute
  derivative increments, `absolute(n)` limits, `++parcov` / `++base_jacobian` / `++ies_par_en`)
  are reported.
- `fill_parval` (`build --fill_parval`, `parrep`), `tempchek --par` and `hpstart --par` convert a
  `.par` value written under a different `SCALE` / `OFFSET` than the control file's through the
  model value, instead of copying `PARVAL1` across. The test fixture's `demo.par` now records the
  parameters' own offsets, as PEST writes them.
- Writing a control file no longer fails on an adjustable `PARVAL1` outside its bounds (a `parrep`
  from an IES realization, say): it is a warning, as documented, and the file is written.
  `makepst validate` still reports it as an error. `diff` normalizes a workbook side, as `build`
  would, so a workbook compares equal to the control file built from it.

- `reweight`: balance observation-group contributions equally or to relative target shares,
  multiply selected group weights directly, match weights to the current misfit with
  `--discrepancy group|obs` (group phi = weighted-observation count, or `w = min(w, 1/|r|)`), consume `.res`/IES observation results, enforce
  optional weight bounds with `--clip`, and write an auditable before/after report.

- `++` options are checked against PEST++ 5.2.29's option list (`makepst/pestpp.py`): a known option
  with a value PEST++ would not accept, or the same option twice (also through an alias), is an error;
  an unknown option is a warning with the closest known name (`did you mean "ies_num_reals"?`) and is
  kept as written. Findings name the workbook cell, csv line or control-file line the option came from
  (`PP!A17`). `validate` reports them; `build`, `parrep` and `set` print them but still write.

- Unknown control-file sections are retained verbatim, including their original header,
  body lines and position, instead of being dropped during a read/write round trip.
- `Pst.validate()` now returns a read-only report; `apply_fixes(report)` and `normalize()` make
  the previous tied-parameter, unused-group and prior-information cleanups explicit.

## 0.3.0 (2026-09-21)

- `log`: ledger of every manifest under a folder — when, which command, which output from
  which sources, dimensions, version; `--file` filters by file name, path or sha256 prefix
  ("which runs used that version of the workbook?"), `--command`, `--last`, `--check`
  verifies every recorded hash
- `bundle`: zip a run for reproduction or review — control file, manifest, templates,
  instruction files, model inputs, command-line files; `--sources`, `--outputs`, `--extra`,
  `--list`, `--strict`; the zip gets its own manifest
- `provenance`: compare the hashes recorded in a manifest with the files on disk
  (`unchanged` / `changed` / `missing`; exit 1 unless all unchanged)
- `update --dry_run`: preview matched names, target columns and skipped cells without writing
- `build` warns when a referenced sheet has formulas without cached values (a workbook saved
  by openpyxl and not yet recalculated in Excel)
- `hpstart`: a PEST_HP `.hp` file with observation values from residuals or an IES realization

## 0.2.0 (2026-09-19)

- `update`: the xlwings backend reads only headers, name and target columns, writes contiguous
  cells in one call per run, suspends events and calculation while writing and recalculates
  once before saving (1,068 parameters over eight sheets of a 27-sheet workbook: ~9 s, was
  minutes); `--par` with `--pst` no longer rewrites the observation sheets
- `tempchek`: check a template file or write model input files from parameter values, with
  PEST's number writer (maximum precision in the space, `PRECIS` / `DPOINT`, one word per
  parameter, right-justified; checked against `tempchek.exe`); for a template + `.par` file
  or for a whole case
- `inschek`: check an instruction file or read a model output file with it into an `.obf`
  file, for one file or a whole case
- `validate`: values that cannot be written into their template space are errors; a line
  advance not at the start of an instruction line and `t` / fixed instructions moving left
  are errors (INSCHEK's rules); three more pestchek warnings (empty observation group,
  `ICOV`/`ICOR`/`IEIG` with more than 300 adjustable parameters, PEST_HP-only variables —
  the last as a note); an all-zero-weight observation group is now a note, not a warning
- `update`: cells inside array formulas or dynamic-array spill ranges are left alone (Excel
  refuses them; xlwings no longer shows a dialog); names present on only one side are reported
  with counts and examples
- `validate`: pestchek's rules added (parameter data, groups, observations, prior information,
  ~60 control-variable ranges and consistency rules, template and instruction syntax), derived
  from PEST 17's `pestchek.F` / `cheksub.F`; file paths resolved from the directory PEST runs
  in, not only the control file's folder

## 0.1.0 (2026-09-19)

First packaged release; replaces the single-file `makePst.py` script.

- `init`: starter workbook with headers, defaults, descriptions, drop-down lists, comments and
  a `BUILD` sheet
- `build`: Excel/CSV tables -> control file; sheet globs (`book,PAR_*`); a workbook with a
  `BUILD` sheet as the only argument; counts computed; PEST_HP keyed tokens and
  `++` options; regularisation equations from `PRIOR`/`WEIGHT` columns; validation of tied
  targets, groups, prior references and bounds
- `dump`: control file -> workbook with a `BUILD` sheet that rebuilds it
- `update`: `.par` / `.res` / `.rei` / PESTPP-IES ensembles into an existing workbook, matched
  by name across sheets; formula cells protected; `--sheet` / `--group` filters; `--real`;
  `PHI` sheet (objective function by observation group) and `PHI_IES` (realization phis)
  whenever residuals are available
- `validate`: pestchek-style report on a control file or workbook: table checks, missing
  files, parameter / observation names against template and instruction files, optional run
  of instruction files against model outputs; exit 1 on errors
- PEST++ version-2 control files (`pcf version=2`, keyword control data, external csv
  tables) are read and written (`--v2`); observation covariance files are kept
- `diff`: semantic comparison of two control files / workbooks (tables, effective control
  values, options, io, comments), text report or workbook; exit 1 on differences
- pyEMU bridge: `to_pyemu()` / `from_pyemu()` via a temporary control file (`pyemu` extra)
- provenance: every producing command writes `<output>.manifest.json` (version, command
  line, source hashes and sheets, output hash and counts)
- `parrep`: `.par` or IES realization into a control file (or a workbook with a `BUILD` sheet),
  with `--set NAME=VALUE`
- Fixes over the old script: `jacupdate` was never written; values were truncated to 6
  significant digits; `phistopthresh` could not be set; SVD / regularisation values in the
  CONTROL sheet were ignored; prior information in estimation mode produced an invalid file
