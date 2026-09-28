# makePst: A Reproducible and Auditable Spreadsheet Workflow for PEST Model Calibration

**Gengxin Ou** (corresponding author; mou@sspa.com), **Zhaowei Wang** (JWang@sspa.com)
S.S. Papadopulos & Associates, Inc.; 1801 Rockville Pike, Suite 220, Rockville, MD 20852

---

## The problem: two representations of one calibration

Parameter estimation with the PEST family of software — PEST (Doherty 2015), PEST_HP (Doherty 2020), and PEST++ (White et al. 2020) — is routine in groundwater modeling. Every run is defined by a control file specifying parameters and their bounds, observations and their weights, parameter and observation groups, prior information, the model command, and the template and instruction files that connect PEST to the model. In the classic format, these definitions occupy sections of one text file, which can run to tens of thousands of lines for a highly parameterized model. PEST++ also supports the `version=2` format, in which tabular data, including parameter and observation data, can be stored in external CSV files. Either format requires the calibration definitions to remain consistent with the modeler's source data and settings.

For such applications, control files are rarely practical to maintain manually, and in many consulting workflows the working calibration record is instead kept in spreadsheets. A workbook can bring parameter definitions, observation values, source references, weighting formulas, and PEST/PEST++ settings into one reviewable record. Sheets can separate parameter types and observation sources, while helper columns retain model layer or pilot-point indices, native values, and notes on data selection. Formulas can balance observation groups, down-weight a period of record, or scale a parameter bound from a native value. This organization lets colleagues review the calibration inputs alongside the assumptions used to prepare them; the control file is derived from that record.

pyEMU (White et al. 2016) supports scripted parameterization, control-file construction, and observation reweighting, so there is overlap with makePst in preparing calibration inputs. makePst focuses on teams that maintain and review these inputs in spreadsheets. The workbook holds source references, formulas, annotations, and control settings alongside parameter and observation tables, allowing colleagues to inspect calibration decisions without tracing the supporting Python scripts. Command-line operations connect this shared record to the control file and return results to it, while manifests and a project log record the files and commands used in that exchange. This workflow extends access to calibration review and its audit trail to team members who do not routinely use Python.

Converting the workbook's calibration tables and settings into a PEST control file can introduce inconsistencies. Counts in the control file must match the tables; parameter groups must be defined before they are used; a tied parameter must point at a parameter that exists and is neither tied nor fixed; a regularization equation must not cite a fixed parameter; every parameter must appear in a template file and every observation in an instruction file. Some inconsistencies prevent a PEST run from starting; others leave a syntactically valid control file whose contents no longer reflect the modeler's intended setup. After a run the traffic reverses: best parameter values, residuals, and — for PESTPP-IES — a chosen realization must go back into the workbook, by name, into the right rows of the right sheets, without overwriting the formulas that produced the weights.

A PEST_HP or PESTPP-IES run for a regional transient model occupies a cluster for hours to days. Errors caught at start-up cost little, whereas an internally valid but unintended configuration — a parameter fixed that should have been adjustable, a weight a formula would have set differently, an earlier iteration's values — can consume a complete run before review exposes the problem. Later, when a reviewer asks which workbook produced a given control file, or what changed between two runs that behaved differently, a text comparison of 40,000 lines is not an answer. Three needs follow: to check a configuration before PEST sees it, to compare two configurations by meaning rather than by text, and to record what produced each file.

Development of makePst was motivated by experience with a conversion script used for a regional model through more than a dozen calibration runs: it wrote the control file, but met none of those needs, and results went back into the workbook by hand. What was missing was a layer that treats the spreadsheet as a first-class, bidirectional interface — keeping it and the control file demonstrably consistent, and preserving the user's formulas when results are written back.

## What makePst does

makePst (installed and invoked as `makepst`) is an open-source Python package operated through a command-line interface. After installing Python and the package, users can run the workbook workflow from a terminal without writing a custom Python script. Installed Excel is not required to read a workbook or build from CSV tables; formula recalculation and the optional xlwings update backend have separate requirements, described below. The package uses one structured representation for calibration tables, control variables, model commands, template and instruction pairs, and PEST++ options, so the same consistency rules apply in both directions (Figure 1).

For example, the following commands create a starter workbook and build a control file in a new working directory:

```text
makepst init calibration.xlsx
makepst build calibration.xlsx --out calibration.pst
```

The starter includes example tables and a `BUILD` sheet containing the command and worksheet mappings used by the second command. Users adapt these definitions and supply their model files, templates, and instructions for an application. This example creates calibration input files; it does not run a model or perform calibration.

Two design decisions underpin auditability. Commands such as `build`, `dump`, and `update` write a sidecar manifest by default, recording software versions, the command line, input files and their hashes, source sheets where applicable, and output information. This record allows a control file to be traced to its source workbook. Round-tripping is defined semantically for supported content: formatting, case, numeric representation, and some ordering may be standardized.

**`build`** reads Excel worksheets or CSV files and writes a PEST-family control file. Parameter and observation tables may span worksheets, alongside control variables, PEST++ options, and model input/output mappings. Counts are calculated automatically; tied parameters, regularization equations generated from preferred values, and PEST_HP keywords are supported.

Input tables follow the layouts in Table 1, with headers in the first row. Columns are identified by case-insensitive headers or by position, depending on the table. Data-sheet names are user-selected and mapped through options such as `--add_par_xls calibration.xlsx,PAR_HK`. The workbook shortcut reads these mappings from a sheet named `BUILD`. Unrecognized helper columns in parameter and observation tables remain in the workbook and are excluded from the control file.

**Table 1.** Input-table roles and core fields. Worksheet and CSV names are examples, not required names. Optional rows are used only when the corresponding definitions are supplied. The [versioned input-schema documentation](https://github.com/ougx/makePst/blob/v0.3.0/README.md#build--tables--pst) describes additional fields and options.

| Table role | Example worksheet / CSV | Core fields or content |
|---|---|---|
| Control settings | `CONTROL` / `control.csv` | Names in column 2, values in column 4; the starter uses `LINE`, `NAME`, `DEFAULT`, `VALUE`. Blank values use built-in defaults where defined. |
| Parameter groups | `PARGP` / `pargp.csv` | `PARGPNME`, `INCTYP`, `DERINC`, `DERINCLB`, `FORCEN`, `DERINCMUL`, `DERMTHD` |
| Parameters | `PAR` or `PAR_HK` / `par.csv` | `PARNME`, `PARTRANS`, `PARCHGLIM`, `PARVAL1`, `PARLBND`, `PARUBND`, `PARGP`, `SCALE`, `OFFSET`, `DERCOM`; optional `TIETO`, `PRIOR`, and `WEIGHT` |
| Observations | `OBS` or `OBS_HEAD` / `obs.csv` | `OBSNME`, `OBSVAL`, `WEIGHT`, `OBGNME` |
| Model interface | `IO` / `io.csv` | First three columns: type (`cmd`, `tpl`, or `ins`), input/command, output; conventionally headed `TYPE`, `IN`, `OUT` |
| PEST++ options (optional) | `PP` / `pp.csv` | First two columns: option name and value, conventionally headed `PP_VAR` and `VAL` |
| Prior information (optional) | `PRIOR` / `prior.csv` | `PINME`, `EQ`, `WEIGHT`, `OBGNME` |
| Parameter ties (optional) | `TIED` / `tied.csv` | First two columns: parameter name and the name of its target; an alternative to `TIETO` in the parameter table |
| Observation-group metadata (optional) | `OBSGP` / `obsgp.csv` | `OBGNME`, with optional `COVFILE` |

Two additional workbook sheets provide convenience and documentation: `BUILD` stores the build command in its first column, headed `COMMAND` in the starter; a `NOTES` sheet can supply comments through `--add_comment_xls`. These are separate from the calibration tables in Table 1.

`build` also accepts CSV tables without a workbook or installed Excel. Each input worksheet can be exported to a separate CSV with the same headers. Repeated `--add_par_csv` or `--add_obs_csv` options combine parameter or observation rows, respectively. CSVs must contain calculated values, such as observation weights, because makePst does not evaluate formulas. The [four-parameter CSV tutorial](https://github.com/ougx/makePst/blob/v0.3.0/README.md#two-minute-tutorial) provides a complete example.

When a workbook is used, formula-derived PEST fields, including observation weights, are read from saved calculated values; `build` does not evaluate formulas. Modelers can revise weighting formulas or their inputs, recalculate and save the workbook, then rebuild the control file.

**`validate`** reports without changing files. It checks duplicate names, groups, ties, bounds, prior equations, and name lengths, then cross-checks parameters and observations against template and instruction files. Optionally, a small interpreter runs the instructions against model output. Validation accepts a workbook before a control file exists and returns a nonzero status on errors, but complements rather than replaces PEST's own checker.

**`dump`** writes supported control-file content to a new workbook that `build` can read back. It does not reconstruct the original workbook’s formulas, annotations, or layout.

**`update`** returns parameter, residual, or PESTPP-IES ensemble results to an existing workbook. It matches rows across worksheets using `PARNME` or `OBSNME`, writes selected fields, and skips target cells containing formulas unless `--overwrite_formulas` is specified. Thus, `MODELLED` and `RESIDUAL` can be updated while a `WEIGHT` formula remains intact. Residual updates also report objective-function contributions by observation group.

Open-source xlwings requires desktop Excel on Windows or macOS ([documentation](https://docs.xlwings.org/en/stable/installation.html)) and recalculates formulas when saving. The openpyxl backend requires no Excel and does not recalculate formulas.

**`parrep`** writes parameter values or an ensemble realization into a new control file.

**`diff`** compares two control files, or a file and workbook, semantically and with numeric tolerances.

**`provenance`** compares the input and output hashes recorded in a manifest with the files on disk, reporting changed or missing files. Unlike `validate`, it checks file identity rather than PEST content. **`log`** lists every manifest under a project folder as a ledger — when, which command, which output from which sources — and can be filtered by file name or hash, so the question "which runs used that version of the workbook?" is answered from the command line.

**`init`** writes a starter workbook that already builds a valid control file.

## Demonstration on a regional model

The demonstration uses the calibration workbook of a regional transient MODFLOW model; the workbook is included with the release, and the accompanying script reproduces the build, round-trip, and openpyxl-update metrics reported below. makePst reads 20 sheets: control variables, parameter groups, eight parameter sheets (horizontal and vertical hydraulic conductivity, specific storage, specific yield, streambed conductance, boundary heads, recharge multipliers, and miscellaneous parameters), seven observation sheets spanning steady-state and transient heads, transient head differences, streamflow, stream leakage, and lake stages, an input/output sheet, and two PEST++ option sheets. Many observation weights are formulas that look up the number of records per well and a group-balancing factor.

*Build.* One command produces a 2.6-MB control file of 43,549 lines — 1,068 parameters in 12 groups, 41,747 observations in 12 groups, 159 regularization equations generated from the parameter sheets, 18 template and 7 instruction files — in about ten seconds on a laptop, most of it spent reading Excel. The manifest records the workbook's hash and the 20 sheet names.

*Round trip.* `dump` writes the control file to a fresh workbook; `build` from that workbook's stored command regenerates the control file; `diff` reports no semantic differences between the original and the rebuilt configuration. For this project the two makePst-generated files were also byte-identical. This tests internal consistency, not whether the original build captured every intended workbook value.

*Validation.* `makepst validate` and PEST's own checker, pestchek 17.5, both report no errors on the built file and agree on their shared warnings: three parameter groups containing only fixed or tied parameters, an observation group that is listed but empty, `MAXSING` above the number of adjustable parameters, and the memory saving available from switching off covariance output. pestchek additionally warns that four control variables are PEST_HP-specific; makePst treats them as supported and reports them as a note. Beyond pestchek, makePst checks that the referenced template, instruction, and model files and the model command exist. In a second project with 164 adjustable parameters, both programs identified the same error: a factor-limited parameter whose bounds differ in sign.

*Results back.* `update` with a parameter file and a residuals file writes values into the eight parameter sheets (1,068 rows matched) and modelled values and residuals into the seven observation sheets (41,747 rows), plus an objective-function sheet, leaving every weight formula untouched. With the xlwings backend, the parameter update of the production workbook itself — 27 worksheets, macros, a chart, and 12,719 formulas on the parameter sheets — took 9.7 s; the saved copy kept every worksheet, formula, and macro, and its 1,068 `PARVAL1` values equal the parameter file's.

**Table 2.** Demonstration workflow and wall-clock times on a Windows 11 laptop with Python 3.12.

| Step | Command | Time | Result |
|---|---|---|---|
| Workbook → control file | `makepst build` | 7–12 s | 1,068 par, 41,747 obs, 159 prior; 43,549 lines |
| Control file → workbook | `makepst dump` | 2–6 s | 9 sheets, incl. the build command |
| Workbook → control file | `makepst build` | 2–5 s | byte-identical to the original |
| Original vs rebuilt | `makepst diff` | ~1 s | no differences |
| Results → workbook copy (openpyxl) | `makepst update` | 12–26 s | 1,068 + 41,747 rows; formulas untouched |
| Parameters → production workbook (xlwings, Excel) | `makepst update --backend xlwings` | 9.7 s | 1,068 rows in 8 of 27 sheets; macros, chart, formulas kept |

## Validation, scope, and limitations

The test suite at the submitted version contains 154 tests, two of which need the demonstration workbook and are skipped without it; continuous integration runs the suite on Linux and Windows with Python 3.9, 3.11, and 3.13. Tests include golden-file builds from a synthetic workbook, read–write identity, `dump`-then-`build` equality, multi-sheet workbook updates with formula protection, provenance checks, ensemble selection, validation rules, the instruction interpreter, and the documented tutorial.

makePst reads and writes classic PEST control files, the PEST_HP extensions used in the demonstration, and PEST++ control files in both the classic and the version-2 external-table formats; it reads `.par`, `.res`/`.rei`, and PESTPP-IES ensemble and objective-function files. It does not perform calibration, uncertainty analysis, geostatistical parameterization, or run management. The instruction interpreter behind `validate` follows the manual but is not PEST, and its findings are reported as warnings; the documentation recommends running pestchek on every generated file. Recognized pass-through sections are retained verbatim; unrecognized sections are dropped with a warning. `dump` places supported control-file content in standardized sheets; the sheets present depend on that content. It does not reconstruct the original workbook's organization, formulas, or notes. Excel-faithful updates require Excel and xlwings; the openpyxl backend keeps macros but not charts and does not recalculate formulas, so a workbook it has updated must be opened, recalculated, and saved in Excel before it is built from again — otherwise cached formula values may be missing or stale. `build` warns about missing cached results in referenced fields but cannot determine whether existing cached values are current.

pyEMU provides a broader Python framework for PEST++ interface construction, parameterization, ensembles, geostatistics, and uncertainty analysis (White et al. 2016). makePst's narrower role is to support workbook-based preparation and review, return results to that record, and retain an audit trail of source files and commands through manifests and the project log. A complementary workflow can use pyEMU to construct the model interface and analyze calibration results, while makePst exposes supported calibration tables, weighting formulas, and control settings for shared workbook review. Exchange occurs through supported PEST-family files. This division is a workflow choice; it does not imply that the packages have no overlapping capabilities, and interchange should not be assumed to preserve every specialized control setting.

## Significance for practice

The immediate practical benefit is a shared place to review the calibration setup: parameter definitions, observation values and source references, weighting assumptions, and PEST/PEST++ settings can be inspected together. A modeler can revise a weighting factor, review the recalculated weights alongside the observations, and rebuild the control file from the saved workbook. Results return by name for the next review cycle, with existing weight formulas protected by default. This keeps the reasoning behind a configuration close to the values supplied to PEST.

The same workflow supports reproducibility. A workbook and its build command can regenerate a configuration, `diff` can check semantic agreement, and manifests record which files produced each output. The workbook centralizes calibration tables and settings; model files, templates, and instruction files remain separate, linked through the input/output mappings. CSV input also supports automated workflows where a spreadsheet interface is unnecessary.

By treating spreadsheets and control files as reproducibly linked representations of the same calibration configuration, makePst lets groundwater modelers retain familiar review practices while making control-file construction, checking, and result transfer reproducible. It is intended as a small, auditable layer within existing PEST and PEST++ workflows, not a replacement for any part of them.

## Software availability

makePst is available under the MIT License at https://github.com/ougx/makePst. The version described here is 0.3.0 (tagged `v0.3.0`; also on PyPI as `makepst`). The repository contains a four-parameter tutorial and the script used for the build, round-trip, and openpyxl-update metrics (`paper/reproduce.py`). The regional-model workbook used for the demonstration is included in the GitHub release assets.

## Conflict of Interest

The authors are developers of makePst. The software is open source under the MIT License.

## References

Doherty, J. 2015. *Calibration and Uncertainty Analysis for Complex Environmental Models.* Brisbane, Australia: Watermark Numerical Computing.

Doherty, J. 2020. *PEST_HP: PEST for Highly Parallelized Computing Environments.* Brisbane, Australia: Watermark Numerical Computing.

White, J.T., M.N. Fienen, and J.E. Doherty. 2016. A python framework for environmental model uncertainty analysis. *Environmental Modelling & Software* 85: 217–228. https://doi.org/10.1016/j.envsoft.2016.08.017

White, J.T., R.J. Hunt, M.N. Fienen, and J.E. Doherty. 2020. *Approaches to Highly Parameterized Inversion: PEST++ Version 5, a Software Suite for Parameter Estimation, Uncertainty Analysis, Management Optimization and Sensitivity Analysis.* U.S. Geological Survey Techniques and Methods 7-C26. https://doi.org/10.3133/tm7C26

---

![Diagram](figure1.png)
**Figure 1.** makePst workflow linking spreadsheet-based project records and PEST-family control files. Calibration definitions are built into and reconstructed from control files through a common structured representation; validation and semantic comparison support QA/QC, while parameter, residual, and ensemble results are returned to existing workbooks by name. The inset is an abbreviated manifest from the demonstration build; complete manifests record software, commands, timestamps, source sheets and hashes, output hashes, and project dimensions. [Figure file: `paper/figure1.svg`, generated by `paper/make_figure1.py`.]
