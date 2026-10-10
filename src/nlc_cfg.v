`default_nettype none

// Configuration register file. Written through (cfg_we, cfg_addr, cfg_data).
// Lossy mode only (D1): enable, number of channels, their ADC slots; DFT debug mode (D11).
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
    output wire [7:0]           rdata,       // readback: the register at cfg_addr (DFT)
    // DFT debug modes (D11): static between config writes, 0 in normal mode
    output wire                 dbg_raw,     // mode 1: raw bypass
    output wire                 dbg_icg,     // mode 2: clock-gate enable observation
    output wire [15:0]          dbg_hot,     // mode 2: one-hot gate group, else 0
    output wire [7:0]           dbg_obs      // this block's gate enables in the selected group
);
`include "nlc_regs.vh"

  reg  [1:0] dbg_mode;
  reg  [3:0] dbg_grp;

  // Readback (DFT, TT pins: project.v): CTRL = {enable, 7'b0} (the mode bits are not
  // stored), N_SEL, DBG, the slot registers; other addresses read 0. Combinational from
  // cfg_addr.
  wire [7:0] slot_off = cfg_addr - REG_SEL_SLOT;
  assign rdata = (cfg_addr == REG_CTRL)  ? {enable, 7'b0} :
                 (cfg_addr == REG_N_SEL) ? {{(7-SEL_W){1'b0}}, n_sel} :
                 (cfg_addr == REG_DBG)   ? {dbg_grp, 2'b00, dbg_mode} :
                 (slot_off < N_SEL)      ? sel_slots[8*slot_off[SEL_W-1:0] +: 8] : 8'd0;

  // Slot registers: storage written before use (hosts write the slots before setting
  // enable, nlc_regs.vh), so no reset; one gated register each (plain flops, no hold mux).
  wire [N_SEL-1:0] slot_we;
  genvar g;
  generate
    for (g = 0; g < N_SEL; g = g + 1) begin : g_slot
      assign slot_we[g] = rst_n && cfg_we && cfg_addr == REG_SEL_SLOT + g;
      nlc_greg #(.W(8)) u_slot (.clk(clk), .en(slot_we[g]),
                                .d(cfg_data), .q(sel_slots[8*g +: 8]));
    end
  endgenerate
  wire [7:0] slot_we8 = slot_we;              // one gate group (N_SEL <= 8)

  // control registers: clock-gated, asynchronous reset (a synchronous one goes through
  // logic that can stay X in gate-level sim). DBG is written only while enable = 0, so the
  // debug mode never changes under a running core (the gated clears built from it in
  // nlc_core stay glitch-free: enable and the mode never change at the same edge).
  wire we_ctrl = cfg_addr == REG_CTRL;
  wire we_nsel = cfg_addr == REG_N_SEL;
  wire we_dbg  = cfg_addr == REG_DBG && !enable;
  wire cg_en   = cfg_we && (we_ctrl || we_nsel || we_dbg);
  wire gclk;
  nlc_icg u_cg (.clk(clk), .en(cg_en), .gclk(gclk));

  always @(posedge gclk or negedge rst_n) begin
    if (!rst_n) begin
      enable   <= 1'b0;
      n_sel    <= 1;
      dbg_mode <= DBG_NORMAL;
      dbg_grp  <= 4'd0;
    end else if (we_ctrl) begin
      enable <= cfg_data[7];                                 // [1:0] mode: ignored
    end else if (we_nsel) begin
      n_sel <= cfg_data[SEL_W:0];
    end else begin                                           // we_dbg
      dbg_mode <= cfg_data[1:0];
      dbg_grp  <= cfg_data[7:4];
    end
  end

  assign dbg_raw = dbg_mode == DBG_RAW;
  assign dbg_icg = dbg_mode == DBG_ICG;
  assign dbg_hot = dbg_icg ? 16'd1 << dbg_grp : 16'd0;

  // gate groups (scripts/dft/icg_map.py): 14 = the slot registers, 15 bit 0 = u_cg
  assign dbg_obs = ({8{dbg_hot[14]}} & slot_we8) | ({8{dbg_hot[15]}} & {7'd0, cg_en});

endmodule
