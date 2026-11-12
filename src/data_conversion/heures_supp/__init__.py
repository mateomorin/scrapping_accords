"""
Structure-aware conversion pipeline for the "Heures supplémentaires
(contingent, majoration)" subset of French company agreements.

Layout:
    core/     shared library (config, utils, structure, triage,
              doc_process, extract/ format extractors)
    steps/    CLI entry points (step0_select ... step5_validate)

See HEURES_SUPP.md for the full data contract.
"""
