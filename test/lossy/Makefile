# nlc_lossy (src/nlc_lossy.sv + nlc_lift53 + nlc_rans + ROM) vs LossyCodec (model/nlc/lossy.py)
#   make -C test/lossy                                   RTL
#   make -C test/lossy GATES=yes NETLIST=<netlist.v>     gate level (sky130 cell models in $NLC_PDK)
#   CLK_NS=200 ...                                       clock period for the test (default 10)
#   PLUSARGS=+vcd=<file> (gate level)                    dump top-level nets for power

SIM ?= icarus
TOPLEVEL_LANG ?= verilog
SRC = $(CURDIR)/../../src
COMPILE_ARGS += -g2012 -I$(SRC)
TOPLEVEL = nlc_lossy
COCOTB_TEST_MODULES ?= test_lossy

ifneq ($(GATES),yes)
SIM_BUILD = sim_build/rtl
VERILOG_SOURCES = $(SRC)/nlc_lossy.sv $(SRC)/nlc_lift53.sv $(SRC)/nlc_rans.sv $(SRC)/nlc_lossy_rom.v \
                  $(SRC)/nlc_icg.sv
else
NLC_PDK ?= /pdk
SIM_BUILD = sim_build/gl
VERILOG_SOURCES = $(NETLIST) $(NLC_PDK)/verilog/primitives.v $(NLC_PDK)/verilog/sky130_fd_sc_hd.v \
                  $(CURDIR)/gl_dump.v
COMPILE_ARGS += -DFUNCTIONAL -DUNIT_DELAY=\#1 -s gl_dump
endif

include $(shell cocotb-config --makefiles)/Makefile.sim
