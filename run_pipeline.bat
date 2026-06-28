@echo off
setlocal EnableExtensions EnableDelayedExpansion

cd /d "%~dp0"

set "COMPOSE_FILE=docker-compose.dev.yml"
set "METRICS_POLL_INTERVAL_SEC=5"

if "%1"=="--no-build" (
  set "BUILD_FLAG="
) else (
  set "BUILD_FLAG=--build"
)

echo [1/6] Starting services...
docker compose -f "%COMPOSE_FILE%" up -d %BUILD_FLAG%
if errorlevel 1 goto :fail

echo [1.5/6] Waiting for Kafka readiness...
set "KAFKA_READY=0"
for /L %%i in (1,1,20) do (
  docker compose -f "%COMPOSE_FILE%" exec -T kafka kafka-topics --bootstrap-server kafka:9092 --list >nul 2>nul
  if not errorlevel 1 (
    set "KAFKA_READY=1"
    goto :kafka_ready
  )
  timeout /t 2 /nobreak >nul
)
:kafka_ready
if not "%KAFKA_READY%"=="1" (
  echo Kafka did not become ready in time.
  goto :fail
)

echo Ensuring Kafka topic reconfig.tasks exists...
docker compose -f "%COMPOSE_FILE%" exec -T kafka kafka-topics --bootstrap-server kafka:9092 --create --if-not-exists --topic reconfig.tasks --partitions 1 --replication-factor 1 >nul
if errorlevel 1 goto :fail

echo Restarting decision-engine after Kafka readiness...
docker compose -f "%COMPOSE_FILE%" restart decision-engine >nul
if errorlevel 1 goto :fail
timeout /t 3 /nobreak >nul

echo [2/6] Preparing PostgreSQL schema and seed data...
set "SQL_FILE=%TEMP%\avlan_init_%RANDOM%%RANDOM%.sql"
(
  echo CREATE TABLE IF NOT EXISTS devices ^(
  echo   id INT PRIMARY KEY,
  echo   hostname TEXT NOT NULL,
  echo   management_ip TEXT NOT NULL,
  echo   os_family TEXT NOT NULL,
  echo   device_type TEXT NOT NULL,
  echo   credential_id INT NOT NULL
  echo ^);
  echo.
  echo CREATE TABLE IF NOT EXISTS vlans ^(
  echo   vlan_id INT PRIMARY KEY,
  echo   name TEXT NOT NULL,
  echo   description TEXT
  echo ^);
  echo.
  echo CREATE TABLE IF NOT EXISTS vlan_assignments ^(
  echo   id INT PRIMARY KEY,
  echo   device_id INT NOT NULL REFERENCES devices^(id^),
  echo   port TEXT NOT NULL,
  echo   vlan_id INT NOT NULL REFERENCES vlans^(vlan_id^),
  echo   mode TEXT NOT NULL
  echo ^);
  echo.
  echo TRUNCATE vlan_assignments, devices, vlans RESTART IDENTITY CASCADE;
  echo.
  echo INSERT INTO devices ^(id, hostname, management_ip, os_family, device_type, credential_id^) VALUES
  echo ^(1, 'sw-1', '10.0.0.1', 'ios', 'SWITCH', 1^),
  echo ^(2, 'sw-2', '10.0.0.2', 'ios', 'SWITCH', 1^);
  echo.
  echo INSERT INTO vlans ^(vlan_id, name, description^) VALUES
  echo ^(10, 'users', 'users vlan'^),
  echo ^(20, 'servers', 'servers vlan'^),
  echo ^(30, 'guests', 'guests vlan'^);
  echo.
  echo INSERT INTO vlan_assignments ^(id, device_id, port, vlan_id, mode^) VALUES
  echo ^(1, 1, 'Gi0/1', 20, 'ACCESS'^),
  echo ^(2, 1, 'Gi0/2', 20, 'ACCESS'^),
  echo ^(3, 1, 'Gi0/3', 10, 'ACCESS'^),
  echo ^(4, 2, 'Gi0/1', 30, 'ACCESS'^),
  echo ^(5, 2, 'Gi0/2', 30, 'ACCESS'^),
  echo ^(6, 2, 'Gi0/3', 20, 'ACCESS'^);
) > "%SQL_FILE%"

docker compose -f "%COMPOSE_FILE%" cp "%SQL_FILE%" postgres:/tmp/avlan_init.sql >nul
if errorlevel 1 goto :fail
docker compose -f "%COMPOSE_FILE%" exec -T postgres psql -U user -d avlan -v ON_ERROR_STOP=1 -f /tmp/avlan_init.sql
if errorlevel 1 goto :fail
docker compose -f "%COMPOSE_FILE%" exec -T postgres rm -f /tmp/avlan_init.sql >nul
if errorlevel 1 goto :fail

echo [3/6] Writing anomaly metrics to VictoriaMetrics...
docker compose -f "%COMPOSE_FILE%" exec -T victoriametrics sh -lc "cat << 'EOF' | curl -sS -X POST --data-binary @- http://localhost:8428/api/v1/import/prometheus > /dev/null
netflow_bytes_total{vlan_id=\"20\",device_id=\"1\",direction=\"in\"} 125000000
netflow_bytes_total{vlan_id=\"20\",device_id=\"2\",direction=\"out\"} 95000000
netflow_flows_total{vlan_id=\"20\",device_id=\"1\"} 7500
netflow_flows_total{vlan_id=\"20\",device_id=\"2\"} 6900
netflow_active_src_ips{vlan_id=\"20\"} 28
netflow_icmp_packets_total{vlan_id=\"20\"} 1600
netflow_intra_vlan_bytes{vlan_id=\"20\"} 21000000
netflow_inter_vlan_bytes{src_vlan_id=\"20\",dst_vlan_id=\"10\"} 1800000
netflow_max_flow_bytes{vlan_id=\"20\",src_ip=\"10.0.20.15\"} 140000000
EOF"
if errorlevel 1 goto :fail

echo [4/6] Waiting for Decision Engine polling cycle...
timeout /t 8 /nobreak >nul

echo [5/6] Recent Decision Engine logs:
docker compose -f "%COMPOSE_FILE%" logs --since 2m decision-engine

echo [6/6] Reading 1 message from Kafka topic reconfig.tasks...
set "TASK_FILE=%TEMP%\avlan_task_%RANDOM%%RANDOM%.json"
docker compose -f "%COMPOSE_FILE%" exec -T kafka kafka-console-consumer --bootstrap-server kafka:9092 --topic reconfig.tasks --from-beginning --timeout-ms 20000 --max-messages 1 > "%TASK_FILE%"
if errorlevel 1 goto :fail
for %%A in ("%TASK_FILE%") do set "TASK_SIZE=%%~zA"
if "%TASK_SIZE%"=="0" (
  echo No task message received from reconfig.tasks.
  goto :fail
)

echo.
echo ===== Reconfiguration Task =====
type "%TASK_FILE%"
echo ================================

del /q "%SQL_FILE%" "%TASK_FILE%" >nul 2>nul

echo.
echo Pipeline completed successfully.
exit /b 0

:fail
echo.
echo Pipeline failed. Check logs above.
if defined SQL_FILE del /q "%SQL_FILE%" >nul 2>nul
if defined TASK_FILE del /q "%TASK_FILE%" >nul 2>nul
exit /b 1
