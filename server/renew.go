package main

import (
    "context"
    "fmt"
    "os"
    "time"
    "github.com/caddyserver/certmagic"
    "github.com/libdns/godaddy"
)

func main() {
    domain := os.Getenv("RELAY_DOMAIN")
    if domain == "" || os.Getenv("GODADDY_API_TOKEN") == "" { os.Exit(2) }
    cfg := certmagic.NewDefault()
    cfg.Storage = &certmagic.FileStorage{Path: "/data/caddy"}
    cfg.Issuers = []certmagic.Issuer{certmagic.NewACMEIssuer(cfg, certmagic.ACMEIssuer{
        CA: "https://acme-v02.api.letsencrypt.org/directory",
        Email: os.Getenv("ACME_EMAIL"), Agreed: true,
        DisableHTTPChallenge: true, DisableTLSALPNChallenge: true,
        DNS01Solver: &certmagic.DNS01Solver{DNSManager: certmagic.DNSManager{
            DNSProvider: &godaddy.Provider{APIToken: os.Getenv("GODADDY_API_TOKEN")},
            PropagationDelay: 30*time.Second, PropagationTimeout: 10*time.Minute,
            Resolvers: []string{"1.1.1.1:53","8.8.8.8:53"},
        }},
    })}
    ctx, cancel := context.WithTimeout(context.Background(), 14*time.Minute)
    defer cancel()
    // Force true requests actual renewal, but never deletes the current certificate.
    if err := cfg.RenewCertSync(ctx, domain, true); err != nil {
        fmt.Fprintln(os.Stderr, "Certificate renewal failed; inspect DNS access and ACME logs.")
        os.Exit(1)
    }
}
