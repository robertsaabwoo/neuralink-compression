`default_nettype none

// Configuration register file. Written through (cfg_we, cfg_addr, cfg_data).
// Lossy mode only (D1): enable, number of channels, their ADC slots.
module nlc_cfg #(
    parameter N_SEL = 8,
    parameter SEL_W = $clog2(N_SEL)
) (
    input  wire                 clk,
    input  wire                 rst_n,
    input  wire                 cfg_we,
    input  wire [7:0]           cfg_addr,
    input  wire [7:0]           cfg_data,
    output reg                  enable,
    output reg  [SEL_W:0]       n_sel,
    output wire [8*N_SEL-1:0]   sel_slots,
    output wire [7:0]           rdata        // readback: the register at cfg_addr (DFT)
);
`include "nlc_regs.vh"

  // Readback (DFT, TT pins: project.v): CTRL = {enable, 7'b0} (the mode bits are not
  // stored), N_SEL, the slot registers; other addresses read 0. Combinational from cfg_addr.
  wire [7:0] slot_off = cfg_addr - REG_SEL_SLOT;
  assign rdata = (cfg_addr == REG_CTRL)  ? {enable, 7'b0} :
                 (cfg_addr == REG_N_SEL) ? {{(7-SEL_W){1'b0}}, n_sel} :
                 (slot_off < N_SEL)      ? sel_slots[8*slot_off[SEL_W-1:0] +: 8] : 8'd0;

  // Slot registers: storage written before use (hosts write the slots before setting
  // enable, nlc_regs.vh), so no reset; one gated register each (plain flops, no hold mux).
  genvar g;
  generate
    for (g = 0; g < N_SEL; g = g + 1) begin : g_slot
      nlc_greg #(.W(8)) u_slot (.clk(clk), .en(rst_n && cfg_we && cfg_addr == REG_SEL_SLOT + g),
                                .d(cfg_data), .q(sel_slots[8*g +: 8]));
    end
  endgenerate

  // control registers: clock-gated, asynchronous reset (a synchronous one goes through
  // logic that can stay X in gate-level sim)
  wire gclk;
  nlc_icg u_cg (.clk(clk), .en(cfg_we && (cfg_addr == REG_CTRL || cfg_addr == REG_N_SEL)),
                .gclk(gclk));

  always @(posedge gclk or negedge rst_n) begin
    if (!rst_n) begin
      enable <= 1'b0;
      n_sel  <= 1;
    end else if (cfg_addr == REG_CTRL) enable <= cfg_data[7];   // [1:0] mode: ignored
    else                               n_sel  <= cfg_data[SEL_W:0];
  end

endmodule
