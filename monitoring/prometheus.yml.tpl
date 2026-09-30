global:
  scrape_interval: 10s
  evaluation_interval: 10s

scrape_configs:
  - job_name: prometheus
    static_configs:
      - targets: ["localhost:9090"]

  - job_name: cadvisor
    static_configs:
      - targets: ["cadvisor:8080"]

  - job_name: backend
    metrics_path: /api/metrics
    authorization:
      type: Bearer
      credentials: "__API_TOKEN__"
    static_configs:
      - targets: ["backend:8000"]
