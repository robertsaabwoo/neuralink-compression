`default_nettype none
`timescale 1ns / 1ps

// nlc_icg: integrated clock gate. gclk pulses with clk in the cycles where en was
// high at the rising edge (en is latched while clk is low: glitch-free gclk).
// Synthesis uses sky130's dlclkp cell (Yosys defines SYNTHESIS; the flow reads
// the liberty so the cell is kept). Simulation uses the same latch + AND, with
// no delay on gclk: flops on gclk sample in the same step as flops on clk.
module nlc_icg (
    input  wire clk,
    input  wire en,
    output wire gclk
);
`ifdef SYNTHESIS
  sky130_fd_sc_hd__dlclkp_1 u_cg (.CLK(clk), .GATE(en), .GCLK(gclk));
`else
  logic en_l;
  always_latch if (!clk) en_l = en;
  assign gclk = clk & en_l;
`endif
endmodule

// nlc_greg: W-bit register written when en is high, clocked through its own
// clock gate (plain flops, no enable mux; idle cycles cost no clock power).
// No reset: for storage that is written before it is read.
module nlc_greg #(
    parameter int W = 1
) (
    input  wire         clk,
    input  wire         en,
    input  wire [W-1:0] d,
    output logic [W-1:0] q
);
`ifdef SYNTHESIS
  `define NLC_GREG_GATED
`endif
`ifdef NLC_ICG_SIM
  `define NLC_GREG_GATED
`endif
`ifdef NLC_GREG_GATED
  wire gclk;
  nlc_icg u_icg (.clk(clk), .en(en), .gclk(gclk));
  always_ff @(posedge gclk) q <= d;
`else
  // RTL simulation: same behaviour as the gate, ~5x faster in Icarus than ~170
  // latch models. The gate itself is simulated at gate level (sky130 dlclkp
  // model) and in RTL with +define+NLC_ICG_SIM.
  always_ff @(posedge clk) if (en) q <= d;
`endif
endmodule
