`default_nettype none
`timescale 1ns / 1ps

// TEST ONLY (test/bringup, never in src/ or the flow): nlc_core with fault hooks, to
// validate scripts/bringup/diagnose.py by fault injection. Same ports as nlc_core, the core
// instance is named `core` as in src/project.v, so hierarchical names match the tool's
// RTL map. Faults on registers are injected from cocotb (Force / Deposit); faults on
// combinational nets need a continuous `force` with an expression, which lives here.
// The design itself is untouched: with fault_sel = 0 nothing is forced.
//
// fault_sel   1  u_l2.d (level-2 lifter detail output) bit fault_bit stuck-at fault_sa
//             2  u_l1.a (level-1 lifter approximation) bit fault_bit stuck-at fault_sa
//             3  slot selector sample register: channels fault_ch_a / fault_ch_b swapped
//             4  slot selector compare: channel fault_ch_a matches slot (configured + 1)
//             5  header byte: seq bit fault_bit stuck-at fault_sa (serialiser output)
//             6  rANS ROM: entry fault_addr = {ctx, sym}, in_fc bit fault_bit flipped
//                (in_fc = {f_s[12:0], c_s[11:0]}; the true value from a shadow ROM)
//             7  rANS write-back wb_x bit fault_bit stuck-at fault_sa (all channels)
module nlc_fault_top (
    input  wire        clk,
    input  wire        rst_n,
    input  wire        s_valid,
    input  wire        s_frame,
    input  wire [9:0]  s_data,
    output wire        s_want,
    input  wire        cfg_we,
    input  wire [7:0]  cfg_addr,
    input  wire [7:0]  cfg_data,
    output wire [7:0]  m_data,
    output wire        m_valid,
    output wire        m_last,
    output wire        m_abort,
    input  wire        m_ready,
    output wire        overflow
);
  nlc_core core (
      .clk(clk), .rst_n(rst_n), .s_valid(s_valid), .s_frame(s_frame), .s_data(s_data),
      .s_want(s_want), .cfg_we(cfg_we), .cfg_addr(cfg_addr), .cfg_data(cfg_data),
      .m_data(m_data), .m_valid(m_valid), .m_last(m_last), .m_abort(m_abort),
      .m_ready(m_ready), .overflow(overflow));

  reg [3:0]  fault_sel  = 4'd0;
  reg [4:0]  fault_bit  = 5'd0;
  reg [7:0]  fault_addr = 8'd0;
  reg        fault_sa   = 1'b0;
  reg [2:0]  fault_ch_a = 3'd0;
  reg [2:0]  fault_ch_b = 3'd0;

  wire [31:0] m1 = 32'd1 << fault_bit;

  // the nets' own RTL expressions (nlc_lift53 / nlc_slot_sel / nlc_lossy), then the fault
  wire [11:0] l2_d = {core.u_enc.u_lossy.u_l2.o_cur[10], core.u_enc.u_lossy.u_l2.o_cur}
                   - {core.u_enc.u_lossy.u_l2.s2[11], core.u_enc.u_lossy.u_l2.s2[11:1]};
  wire [10:0] l1_a = {core.u_enc.u_lossy.u_l1.e[9], core.u_enc.u_lossy.u_l1.e}
                   + core.u_enc.u_lossy.u_l1.t[12:2];
  wire [2:0]  idx3 = core.u_sel.idx[2:0];
  wire [2:0]  swp  = idx3 == fault_ch_a ? fault_ch_b : idx3 == fault_ch_b ? fault_ch_a : idx3;
  wire [7:0]  want_ok = core.u_sel.sel_slots[8 * idx3 +: 8];
  wire [5:0]  seq_f = fault_sa ? (core.u_enc.u_lossy.seq | m1[5:0])
                               : (core.u_enc.u_lossy.seq & ~m1[5:0]);

  // faulty values as wires: Icarus keeps a force continuous only for a plain net RHS
  wire [11:0] f_l2_d = fault_sa ? (l2_d | m1[11:0]) : (l2_d & ~m1[11:0]);
  wire [10:0] f_l1_a = fault_sa ? (l1_a | m1[10:0]) : (l1_a & ~m1[10:0]);
  wire [14:0] f_smp  = {core.u_sel.s_data, swp, core.u_sel.idx == 4'd0,
                        core.u_sel.idx + 4'd1 == core.u_sel.n_sel};
  wire [7:0]  f_want = want_ok + (idx3 == fault_ch_a ? 8'd1 : 8'd0);
  wire [7:0]  f_mdat = core.u_enc.u_lossy.need_hdr ? {2'b01, seq_f}
                     : core.u_enc.u_lossy.c_data[8 * core.u_enc.u_lossy.bi +: 8];

  wire [7:0]  rom_a = {core.u_enc.u_lossy.u_rans.s_tctx, core.u_enc.u_lossy.u_rans.s_tdata};
  wire [24:0] fc_true;
  nlc_lossy_rom u_shadow_rom (.addr(rom_a), .fc(fc_true));
  wire [24:0] f_fc = rom_a == fault_addr ? fc_true ^ m1[24:0] : fc_true;
  wire [21:0] wb_true = {core.u_enc.u_lossy.u_rans.w_dq,
                         core.u_enc.u_lossy.u_rans.w_rem[11:0] + core.u_enc.u_lossy.u_rans.w_c};
  wire [21:0] f_wb = fault_sa ? (wb_true | m1[21:0]) : (wb_true & ~m1[21:0]);

  always @(fault_sel) begin
    release core.u_enc.u_lossy.u_l2.d;
    release core.u_enc.u_lossy.u_l1.a;
    release core.u_sel.u_smp.d;
    release core.u_sel.want;
    release core.u_enc.u_lossy.m_data;
    release core.u_enc.u_lossy.u_rans.in_fc;
    release core.u_enc.u_lossy.u_rans.wb_x;
    case (fault_sel)
      4'd1: force core.u_enc.u_lossy.u_l2.d = f_l2_d;
      4'd2: force core.u_enc.u_lossy.u_l1.a = f_l1_a;
      4'd3: force core.u_sel.u_smp.d = f_smp;
      4'd4: force core.u_sel.want = f_want;
      4'd5: force core.u_enc.u_lossy.m_data = f_mdat;
      4'd6: force core.u_enc.u_lossy.u_rans.in_fc = f_fc;
      4'd7: force core.u_enc.u_lossy.u_rans.wb_x = f_wb;
      default: ;
    endcase
  end
endmodule
