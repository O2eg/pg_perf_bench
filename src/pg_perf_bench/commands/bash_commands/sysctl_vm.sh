#!/bin/sh
/sbin/sysctl -a 2>/dev/null | grep '^vm\.'
