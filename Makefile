COMPOSE ?= docker compose
GPU_COMPOSE = $(COMPOSE) -f compose.yaml -f compose.gpu.yaml

.PHONY: up up-gpu down logs ps status token-add token-revoke package install uninstall

up:
	$(COMPOSE) up -d --build

up-gpu:
	$(GPU_COMPOSE) up -d --build

down:
	$(COMPOSE) down

logs:
	$(COMPOSE) logs -f whisper-server

ps:
	$(COMPOSE) ps

status:
	$(COMPOSE) ps

token-add:
	@test -n "$(USER_ID)" || (echo "Usage: make token-add USER_ID=<friend-or-user-id>" >&2; exit 2)
	$(COMPOSE) run --rm --no-deps whisper-server whisper-server token add --user "$(USER_ID)" --file /data/tokens.json
	@if $(COMPOSE) ps --status running --services | grep -qx whisper-server; then $(COMPOSE) restart whisper-server; fi

token-revoke:
	@test -n "$(USER_ID)" || (echo "Usage: make token-revoke USER_ID=<friend-or-user-id>" >&2; exit 2)
	$(COMPOSE) run --rm --no-deps whisper-server whisper-server token revoke --user "$(USER_ID)" --file /data/tokens.json
	@if $(COMPOSE) ps --status running --services | grep -qx whisper-server; then $(COMPOSE) restart whisper-server; fi

package:
	bash scripts/package.sh

install:
	bash install.sh

uninstall:
	bash uninstall.sh
