# Local verification / implementation-estimate flow (scripts/check.py -> scripts/flow/flow.py):
# Icarus + cocotb 2.1 (RTL and gate-level sim), Verilator (lint, as TT's flow does),
# Yosys (synthesis), OpenSTA (timing, power), sky130_fd_sc_hd libraries (3 corners + models).
#
# Build inputs are fetched on the host (pip/TLS inside containers fails on this machine):
#   python docker/fetch_inputs.py && docker build -t nlc-flow docker
#
# OpenSTA is built from source: Debian's opensta 2.0.17 reports zero internal (incl. clock-pin)
# power and cannot read VCD.

FROM python:3.12-slim-trixie AS sta-build
RUN apt-get update -qq \
 && apt-get install -y -qq --no-install-recommends build-essential cmake tcl-dev swig bison flex libfl-dev \
      libeigen3-dev zlib1g-dev
COPY src/cudd/cudd_versions/cudd-3.0.0.tar.gz /tmp/
RUN cd /tmp && tar xzf cudd-3.0.0.tar.gz && cd cudd-3.0.0 \
 && ./configure --prefix=/opt/cudd >/dev/null && make -j"$(nproc)" >/dev/null && make install >/dev/null
COPY src/OpenSTA /src/OpenSTA
RUN cmake -S /src/OpenSTA -B /build -DCMAKE_BUILD_TYPE=Release -DCUDD_DIR=/opt/cudd \
      -DCMAKE_INSTALL_PREFIX=/opt/sta >/dev/null \
 && cmake --build /build -j"$(nproc)" >/dev/null && cmake --install /build >/dev/null \
 && /opt/sta/bin/sta -version

FROM python:3.12-slim-trixie
RUN apt-get update -qq \
 && apt-get install -y -qq --no-install-recommends iverilog yosys verilator make zstd libtcl8.6 \
 && rm -rf /var/lib/apt/lists/*
COPY --from=sta-build /opt/sta/bin/sta /usr/local/bin/sta

COPY wheels /tmp/wheels
RUN pip install -q --no-cache-dir --no-index --find-links /tmp/wheels cocotb==2.1.0 pytest==8.4.2 numpy \
 && rm -rf /tmp/wheels

# sky130_fd_sc_hd from the ciel (open_pdks) release: keep the three STA corners and the
# gate-level simulation models, drop the rest (GDS, LEF, SPICE, ...).
COPY pdk/sky130_fd_sc_hd.tar.zst /tmp/
RUN mkdir -p /tmp/x && tar --zstd -xf /tmp/sky130_fd_sc_hd.tar.zst -C /tmp/x \
 && L=$(find /tmp/x -type d -path '*libs.ref/sky130_fd_sc_hd' | head -1) \
 && mkdir -p /pdk/lib /pdk/verilog \
 && cp $L/lib/sky130_fd_sc_hd__tt_025C_1v80.lib $L/lib/sky130_fd_sc_hd__ss_100C_1v60.lib \
       $L/lib/sky130_fd_sc_hd__ff_n40C_1v95.lib /pdk/lib/ \
 && cp $L/verilog/primitives.v $L/verilog/sky130_fd_sc_hd.v /pdk/verilog/ \
 && rm -rf /tmp/x /tmp/sky130_fd_sc_hd.tar.zst && ls -la /pdk/lib /pdk/verilog

ENV NLC_PDK=/pdk
WORKDIR /work
