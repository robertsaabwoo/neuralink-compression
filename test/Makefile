# Based on the Tiny Tapeout template test/Makefile.
#   make vectors            regenerate golden vectors from the Python model
#   make                    RTL simulation with your encoder (src/nlc_encoder.v)
#   make ENCODER=replay     harness self-test: replay golden bytes, no real encoder
#   make GATES=yes          gate-level simulation of the hardened netlist (TT GDS action)
#   make GATES=local NETLIST=<top_netlist.v>   gate level of our Yosys netlist (T-GL-2,
#                           nlc-flow image: sky130 models in $NLC_PDK, no power pins)
#   CLK_NS (default 200 = 5 MHz), SLOT_CYCLES (default 2): see harness.py

SIM ?= icarus
FST ?= -fst
TOPLEVEL_LANG ?= verilog
SRC_DIR = $(CURDIR)/../src
PROJECT_SOURCES = project.v nlc_core.v nlc_cfg.v nlc_slot_sel.v nlc_encoder.v nlc_lossy.sv nlc_lift53.sv nlc_rans.sv nlc_lossy_rom.v nlc_icg.sv
ENCODER ?= user
export ENCODER

# golden vectors are generated, not committed: make them on first use (TT's CI runs plain `make`)
ifeq ($(wildcard $(CURDIR)/vectors/lossy/expected.hex),)
$(info generating test/vectors from the golden model)
$(shell cd $(CURDIR)/.. && python scripts/gen_vectors.py --out test/vectors >&2)
endif

ifeq ($(GATES),local)

NLC_PDK ?= /pdk
SIM_BUILD = sim_build/gl_local
COMPILE_ARGS += -DFUNCTIONAL -DUNIT_DELAY=\#1
VERILOG_SOURCES += $(NETLIST) $(NLC_PDK)/verilog/primitives.v $(NLC_PDK)/verilog/sky130_fd_sc_hd.v

else ifneq ($(GATES),yes)

ifeq ($(ENCODER),replay)
SIM_BUILD = sim_build/replay
VERILOG_SOURCES += $(addprefix $(SRC_DIR)/,$(filter-out nlc_encoder.v,$(PROJECT_SOURCES)))
VERILOG_SOURCES += $(CURDIR)/mock/nlc_encoder_replay.v
else
SIM_BUILD = sim_build/rtl
VERILOG_SOURCES += $(addprefix $(SRC_DIR)/,$(PROJECT_SOURCES))
endif

else

SIM_BUILD = sim_build/gl
COMPILE_ARGS += -DGL_TEST
COMPILE_ARGS += -DFUNCTIONAL
COMPILE_ARGS += -DUSE_POWER_PINS
COMPILE_ARGS += -DSIM
COMPILE_ARGS += -DUNIT_DELAY=\#1
VERILOG_SOURCES += $(PDK_ROOT)/sky130A/libs.ref/sky130_fd_sc_hd/verilog/primitives.v
VERILOG_SOURCES += $(PDK_ROOT)/sky130A/libs.ref/sky130_fd_sc_hd/verilog/sky130_fd_sc_hd.v
VERILOG_SOURCES += $(CURDIR)/gate_level_netlist.v

endif

COMPILE_ARGS += -I$(SRC_DIR) -g2012
VERILOG_SOURCES += $(CURDIR)/tb.v
TOPLEVEL = tb
COCOTB_TEST_MODULES = test_plumbing,test_modes

include $(shell cocotb-config --makefiles)/Makefile.sim

.PHONY: vectors
vectors:
	cd .. && python scripts/gen_vectors.py --out test/vectors
