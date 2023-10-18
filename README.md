# SurgeBot

A patch renderer for Surge XT using SurgePy.

## Install

```python
python -m venv surge_env
source ./surge_env/bin/activate
<edit requirements.txt to point surgepy requirement at a local copy of Surge repo>
pip install -r ./requirements.txt

export SURGEBOT_DISCORD_TOKEN=<bot token>
python main.py
```
