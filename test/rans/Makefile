# Unit tests for the rANS encoders against model/nlc/rans_tdm.py
#   make -C test/rans TOP=static      (default)
#   make -C test/rans TOP=adaptive

SIM ?= icarus
TOPLEVEL_LANG ?= verilog
TOP ?= static
COMPILE_ARGS += -g2012
SIM_BUILD = sim_build/$(TOP)
VERILOG_SOURCES = $(CURDIR)/ref/rans_tdm_$(TOP).sv
TOPLEVEL = rans_tdm_$(TOP)
COCOTB_TEST_MODULES = test_rans_$(TOP)

include $(shell cocotb-config --makefiles)/Makefile.sim
