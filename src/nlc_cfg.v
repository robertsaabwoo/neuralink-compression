`default_nettype none

// Configuration register file. Written through (cfg_we, cfg_addr, cfg_data).
module nlc_cfg #(
    parameter N_SEL = 8,
    parameter SEL_W = $clog2(N_SEL)
) (
    input  wire                 clk,
    input  wire                 rst_n,
    input  wire                 cfg_we,
    input  wire [7:0]           cfg_addr,
    input  wire [7:0]           cfg_data,
    output reg  [1:0]           mode,
    output reg                  enable,
    output reg  [SEL_W:0]       n_sel,
    output reg  [7:0]           fpp,
    output reg  [15:0]          win_len,
    output reg  [4:0]           sbp_shift,
    output wire [8*N_SEL-1:0]   sel_slots,
    output wire [8*N_SEL-1:0]   thr
);
`include "nlc_regs.vh"

  reg [7:0] slot_r [0:N_SEL-1];
  reg [7:0] thr_r  [0:N_SEL-1];

  genvar g;
  generate
    for (g = 0; g < N_SEL; g = g + 1) begin : g_flat
      assign sel_slots[8*g +: 8] = slot_r[g];
      assign thr[8*g +: 8]       = thr_r[g];
    end
  endgenerate

  integer i;
  always @(posedge clk) begin
    if (!rst_n) begin
      mode      <= MODE_LOSSLESS;
      enable    <= 1'b0;
      n_sel     <= 1;
      fpp       <= 8'd128;
      win_len   <= 16'd400;
      sbp_shift <= 5'd6;
      for (i = 0; i < N_SEL; i = i + 1) begin
        slot_r[i] <= i;
        thr_r[i]  <= 8'hFF;
      end
    end else if (cfg_we) begin
      case (cfg_addr)
        REG_CTRL:      begin mode <= cfg_data[1:0]; enable <= cfg_data[7]; end
        REG_N_SEL:     n_sel <= cfg_data[SEL_W:0];
        REG_FPP:       fpp <= cfg_data;
        REG_WIN_LO:    win_len[7:0] <= cfg_data;
        REG_WIN_HI:    win_len[15:8] <= cfg_data;
        REG_SBP_SHIFT: sbp_shift <= cfg_data[4:0];
        default: begin
          for (i = 0; i < N_SEL; i = i + 1) begin
            if (cfg_addr == REG_SEL_SLOT + i) slot_r[i] <= cfg_data;
            if (cfg_addr == REG_THR + i)      thr_r[i]  <= cfg_data;
          end
        end
      endcase
    end
  end

endmodule
