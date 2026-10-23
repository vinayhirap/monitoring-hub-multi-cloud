# cloudops-agent: static, no-cgo cross builds for linux amd64 + arm64.
#   make test      go vet + go test -race
#   make dist      dist/cloudops-agent-{linux,darwin}-{amd64,arm64}, cloudops-agent-windows-{amd64,arm64}.exe,
#                  the per-OS install/uninstall scripts and dist/SHA256SUMS (cross-compiles; Go 1.22+ on any host)
#   make vet-all   go vet for every shipped OS/arch (catches a build break on an OS you are not developing on)
#   make clean
VERSION ?= $(shell cat VERSION 2>/dev/null || echo 0.1.0)
DIST    := dist
PKG     := ./cmd/cloudops-agent
LDFLAGS := -s -w -X main.version=$(VERSION)
GOFLAGS_BUILD := -trimpath -buildvcs=false

TARGETS := linux-amd64 linux-arm64 darwin-amd64 darwin-arm64 windows-amd64.exe windows-arm64.exe
BINS    := $(addprefix $(DIST)/cloudops-agent-,$(TARGETS))
SCRIPTS := install.sh uninstall.sh install-macos.sh uninstall-macos.sh install.ps1 uninstall.ps1

.PHONY: all test vet vet-all dist clean checksums
all: dist

vet:
	go vet ./...

vet-all:
	@for t in $(subst .exe,,$(TARGETS)); do os=$${t%-*}; arch=$${t#*-}; echo "vet $$os/$$arch"; CGO_ENABLED=0 GOOS=$$os GOARCH=$$arch go vet ./... || exit 1; done

test: vet
	go test -race -count=1 ./...

# One static, no-cgo binary per OS/arch. %-pattern: the target name is cloudops-agent-<os>-<arch>[.exe].
$(DIST)/cloudops-agent-linux-amd64:
	@mkdir -p $(DIST)
	CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build $(GOFLAGS_BUILD) -ldflags '$(LDFLAGS)' -o $@ $(PKG)

$(DIST)/cloudops-agent-linux-arm64:
	@mkdir -p $(DIST)
	CGO_ENABLED=0 GOOS=linux GOARCH=arm64 go build $(GOFLAGS_BUILD) -ldflags '$(LDFLAGS)' -o $@ $(PKG)

$(DIST)/cloudops-agent-darwin-amd64:
	@mkdir -p $(DIST)
	CGO_ENABLED=0 GOOS=darwin GOARCH=amd64 go build $(GOFLAGS_BUILD) -ldflags '$(LDFLAGS)' -o $@ $(PKG)

$(DIST)/cloudops-agent-darwin-arm64:
	@mkdir -p $(DIST)
	CGO_ENABLED=0 GOOS=darwin GOARCH=arm64 go build $(GOFLAGS_BUILD) -ldflags '$(LDFLAGS)' -o $@ $(PKG)

$(DIST)/cloudops-agent-windows-amd64.exe:
	@mkdir -p $(DIST)
	CGO_ENABLED=0 GOOS=windows GOARCH=amd64 go build $(GOFLAGS_BUILD) -ldflags '$(LDFLAGS)' -o $@ $(PKG)

$(DIST)/cloudops-agent-windows-arm64.exe:
	@mkdir -p $(DIST)
	CGO_ENABLED=0 GOOS=windows GOARCH=arm64 go build $(GOFLAGS_BUILD) -ldflags '$(LDFLAGS)' -o $@ $(PKG)

# Plain "<sha256>  <file>" lines so `sha256sum -c` and install.sh can verify them.
checksums: $(BINS)
	cd $(DIST) && sha256sum $(notdir $(BINS)) > SHA256SUMS

dist: checksums
	@cp $(SCRIPTS) $(DIST)/
	@ls -l $(DIST)

clean:
	rm -rf $(DIST)
