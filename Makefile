# Local machine-specific overrides (gitignored) - copy config.mk.example to
# config.mk and adjust for your setup instead of editing this file.
-include config.mk

PI_HOST    ?= pi@raspberrypi.local
HOST       ?= raspberrypi.local
REMOTE_DIR ?= /home/pi/xair-recorder
SERVICE    ?= xair-recorder
PORT       ?= 8080

RSYNC_EXCLUDES := --exclude='.git' --exclude='__pycache__' --exclude='test-downloads' \
                  --exclude='.claude' --exclude='.DS_Store' --exclude='config.mk'

.PHONY: check test deploy restart status logs ssh

## Run the unit/integration test suite (tests/, stdlib unittest - no deps to install).
test:
	python3 -m unittest discover -s tests -v

## Validate Python compiles, the embedded page JS is syntactically valid, and tests pass.
check: test
	python3 -m py_compile recorder.py webui.py
	@python3 -c "import webui; open('/tmp/xair-page-check.js', 'w').write(webui.PAGE.split('<script>')[1].split('</script>')[0])"
	@node --check /tmp/xair-page-check.js && echo "JS OK"

## Sync the repo to the device and restart the web service. Runs `check` (incl. tests) first.
## --delete keeps the remote in sync with local removals (e.g. a retired
## asset); safe here since recordings/state live outside REMOTE_DIR.
deploy: check
	rsync -av --delete $(RSYNC_EXCLUDES) ./ $(PI_HOST):$(REMOTE_DIR)/
	ssh $(PI_HOST) 'sudo systemctl restart $(SERVICE) && sleep 1 && sudo systemctl status $(SERVICE) --no-pager | head -6'

## Restart the service without re-syncing files.
restart:
	ssh $(PI_HOST) 'sudo systemctl restart $(SERVICE)'

## Show service status and current recorder status.
status:
	ssh $(PI_HOST) 'sudo systemctl status $(SERVICE) --no-pager'
	@curl -s http://$(HOST):$(PORT)/status; echo

## Tail the service log live (Ctrl-C to stop).
logs:
	ssh $(PI_HOST) 'sudo journalctl -u $(SERVICE) -f'

## Open an interactive shell on the device.
ssh:
	ssh $(PI_HOST)
