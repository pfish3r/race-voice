FROM caddy:2-builder AS builder
RUN xcaddy build --with github.com/caddy-dns/godaddy@v1.2.0
WORKDIR /renew
COPY server/renew.go .
RUN go mod init racevoice-renew && go get github.com/caddyserver/certmagic@v0.24.0 github.com/libdns/godaddy@v1.1.0 && go mod tidy && CGO_ENABLED=0 go build -o /usr/bin/racevoice-renew .
FROM caddy:2
RUN apk add --no-cache python3
COPY --from=builder /usr/bin/caddy /usr/bin/caddy
COPY --from=builder /usr/bin/racevoice-renew /usr/bin/racevoice-renew
COPY server/controller.py /opt/racevoice/controller.py
COPY metrics.py /opt/racevoice/metrics.py
CMD ["python3", "/opt/racevoice/controller.py"]
