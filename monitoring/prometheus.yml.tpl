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

  - job_name: blackbox-http
    metrics_path: /probe
    params:
      module: [http_2xx]
    static_configs:
      - targets:
          - http://backend:8000/health
          - http://grafana:3000/api/health
          - http://prometheus:9090/-/healthy
          - http://loki:3100/ready
          - http://weaviate:8080/v1/.well-known/ready
          - http://neo4j:7474
          - http://ollama:11434/api/tags
    relabel_configs:
      - source_labels: [__address__]
        target_label: __param_target
      - source_labels: [__param_target]
        target_label: instance
      - target_label: __address__
        replacement: blackbox:9115

  - job_name: blackbox-tcp
    metrics_path: /probe
    params:
      module: [tcp_connect]
    static_configs:
      - targets:
          - postgres:5432
          - redis:6379
          - t2v-transformers:8080
    relabel_configs:
      - source_labels: [__address__]
        target_label: __param_target
      - source_labels: [__param_target]
        target_label: instance
      - target_label: __address__
        replacement: blackbox:9115
