SHELL := /bin/bash
.DEFAULT_GOAL := help
.PHONY: help check up status inspect verify restore validate report logs down test smoke config tools sql
help:
	@printf '%s\n' 'check up status inspect verify restore validate report logs down' 'restore es una orden explícita de restauración. No sobrescribe bases.' 'sql FILE=sql/consulta.sql | tools (cliente alternativo) | test | config'
check:
	@./scripts/check-requirements.sh
up:
	@./scripts/start.sh
status:
	@python3 scripts/historico.py status
inspect:
	@./scripts/inspect-backups.sh
verify:
	@./scripts/verify-backups.sh
restore:
	@./scripts/restore-backups.sh
validate:
	@./scripts/check-databases.sh
report:
	@./scripts/generate-report.sh
logs:
	@python3 scripts/historico.py logs
down:
	@python3 scripts/historico.py down
config:
	@python3 scripts/historico.py config
tools:
	@python3 scripts/historico.py tools
sql:
	@./scripts/sqlcmd.sh "$(FILE)"
test:
	@python3 -m unittest discover -s tests -v
smoke:
	@python3 tests/smoke_sqlserver.py
