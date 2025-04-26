export ADB_SERVER_SOCKET=tcp:host.docker.internal:5037
REMOTE_DEVICE_HOST=$(getent hosts host.docker.internal | awk '{print $1}')
scrcpy --tunnel-host="$REMOTE_DEVICE_HOST"