FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY netorbit_pi ./netorbit_pi
RUN pip install --no-cache-dir . && useradd --system netorbit && mkdir /data && chown netorbit /data
USER netorbit
ENV NETORBIT_DATA=/data
VOLUME /data
EXPOSE 8099
# Needs the host's network (to see the ARP table and reach every device) and CAP_NET_RAW for ping.
CMD ["netorbit-pi"]
