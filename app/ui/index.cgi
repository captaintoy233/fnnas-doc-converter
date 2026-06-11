#!/bin/bash
# CGI 入口 - fnOS 桌面图标指向此脚本
BASE_PATH="/var/apps/App.DocConverter/target/www"
URI_NO_QUERY="${REQUEST_URI%%\?*}"
REL_PATH="/"
case "$URI_NO_QUERY" in
    *index.cgi*)
        REL_PATH="${URI_NO_QUERY#*index.cgi}"
        ;;
esac
if [ -z "$REL_PATH" ] || [ "$REL_PATH" = "/" ]; then
    REL_PATH="/"
fi
# 反向代理到 FastAPI 服务
echo "Status: 302 Found"
echo "Location: http://localhost:${HOST_PORT:-8080}"
echo ""
