# cloudops-agent: static, no-cgo cross builds for linux amd64 + arm64.
#   make test      go vet + go test -race
#   make dist      dist/cloudops-agent-linux-{amd64,arm64} + dist/SHA256SUMS
#   make clean
VERSION ?= $(shell cat VERSION 2>/dev/null || echo 0.1.0)
DIST    := dist
PKG     := ./cmd/cloudops-agent
LDFLAGS := -s -w -X main.version=$(VERSION)
GOFLAGS_BUILD := -trimpath -buildvcs=false

TARGETS := linux-amd64 linux-arm64
BINS    := $(addprefix $(DIST)/cloudops-agent-,$(TARGETS))

.PHONY: all test vet dist clean checksums
all: dist

vet:
	go vet ./...

test: vet
	go test -race -count=1 ./...

$(DIST)/cloudops-agent-linux-amd64:
	@mkdir -p $(DIST)
	CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build $(GOFLAGS_BUILD) -ldflags '$(LDFLAGS)' -o $@ $(PKG)

$(DIST)/cloudops-agent-linux-arm64:
	@mkdir -p $(DIST)
	CGO_ENABLED=0 GOOS=linux GOARCH=arm64 go build $(GOFLAGS_BUILD) -ldflags '$(LDFLAGS)' -o $@ $(PKG)

# Plain "<sha256>  <file>" lines so `sha256sum -c` and install.sh can verify them.
checksums: $(BINS)
	cd $(DIST) && sha256sum cloudops-agent-linux-amd64 cloudops-agent-linux-arm64 > SHA256SUMS

dist: checksums
	@cp install.sh uninstall.sh $(DIST)/ 2>/dev/null || true
	@ls -l $(DIST)

clean:
	rm -rf $(DIST)
