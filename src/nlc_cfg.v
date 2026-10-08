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
    output wire [8*N_SEL-1:0]   sel_slots
);
`include "nlc_regs.vh"

  reg [7:0] slot_r [0:N_SEL-1];

  genvar g;
  generate
    for (g = 0; g < N_SEL; g = g + 1) begin : g_flat
      assign sel_slots[8*g +: 8] = slot_r[g];
    end
  endgenerate

  // clock-gated: the registers only see a clock edge on a write. Asynchronous reset:
  // a synchronous one goes through logic that can stay X in gate-level sim.
  wire gclk;
  nlc_icg u_cg (.clk(clk), .en(cfg_we), .gclk(gclk));

  integer i;
  always @(posedge gclk or negedge rst_n) begin
    if (!rst_n) begin
      enable <= 1'b0;
      n_sel  <= 1;
      for (i = 0; i < N_SEL; i = i + 1) slot_r[i] <= i[7:0];
    end else if (cfg_we) begin
      case (cfg_addr)
        REG_CTRL:  enable <= cfg_data[7];          // [1:0] mode: ignored (lossy only)
        REG_N_SEL: n_sel  <= cfg_data[SEL_W:0];
        default:
          for (i = 0; i < N_SEL; i = i + 1)
            if (cfg_addr == REG_SEL_SLOT + i[7:0]) slot_r[i] <= cfg_data;
      endcase
    end
  end

endmodule
