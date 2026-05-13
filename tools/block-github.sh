#!/bin/bash
# Block GitHub CDN to prevent urllib freeze
iptables -C OUTPUT -d 79.127.224.0/24 -j REJECT 2>/dev/null || iptables -A OUTPUT -d 79.127.224.0/24 -j REJECT
iptables -C OUTPUT -d 185.199.108.0/22 -j REJECT 2>/dev/null || iptables -A OUTPUT -d 185.199.108.0/22 -j REJECT
