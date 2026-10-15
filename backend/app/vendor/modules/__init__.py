"""Copies of the team's shared pipeline modules (/data/data_eng/py_scripts/modules on the pipeline servers).

The Datapipeline Automation Hub rebuilt these from screenshots of the real files so it can test-run generated
scripts in lab (and smoke-run them offline). They are NOT shipped with generated bundles: on the servers the
generated script puts /data/py_scripts and /data/data_eng/py_scripts first on sys.path, so the real modules win.

Keep their behaviour identical to the originals; point SF_MODULES_DIR at the real folder when it is reachable.
"""
