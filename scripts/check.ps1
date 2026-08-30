$ErrorActionPreference = "Stop"

python -m compileall smallagent
python -m unittest discover -s tests -v
