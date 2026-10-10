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

// nlc_lreg: W-bit latch row (DFFRAM style), the cheaper twin of nlc_greg
// (dlxtp 15.0 vs dfxtp 20.0 um^2 per bit). The row's clock gate opens the latches
// for the high phase of the cycle after en (the same edge an nlc_greg would load),
// so q takes the new value at the same clock edge as a flop would, plus the latch
// delay. Contract (the caller's): d comes straight from registers loaded at that
// same edge (a staging register), so it is stable while the row is open and for
// half a cycle after it closes; d never depends on q of any open row (no loop).
// No reset: storage written before it is read.
module nlc_lreg #(
    parameter int W = 1
) (
    input  wire         clk,
    input  wire         en,
    input  wire [W-1:0] d,
    output logic [W-1:0] q
);
  wire gclk;
  nlc_icg u_icg (.clk(clk), .en(en), .gclk(gclk));
`ifdef SYNTHESIS
  genvar i;
  generate
    for (i = 0; i < W; i++) begin : g_b
      sky130_fd_sc_hd__dlxtp_1 u_l (.D(d[i]), .GATE(gclk), .Q(q[i]));
    end
  endgenerate
`else
  // nonblocking: flops clocked by the edge that opens the row sample the old q
  // (with '=', Icarus could update q before they sample, a zero-delay race).
  // Plain always: Verilator 5.032 reports NOLATCH for always_latch here.
  always @(gclk or d) if (gclk) q <= d;
`endif
endmodule

// nlc_rreg: a storage row, latches (LATCH = 1: nlc_lreg, d must obey its staging
// contract) or flops (LATCH = 0: nlc_greg). Same write timing either way.
module nlc_rreg #(
    parameter int W     = 1,
    parameter bit LATCH = 1'b1
) (
    input  wire         clk,
    input  wire         en,
    input  wire [W-1:0] d,
    output logic [W-1:0] q
);
  generate
    if (LATCH) begin : g_l
      nlc_lreg #(.W(W)) u_r (.clk(clk), .en(en), .d(d), .q(q));
    end else begin : g_f
      nlc_greg #(.W(W)) u_r (.clk(clk), .en(en), .d(d), .q(q));
    end
  endgenerate
endmodule
