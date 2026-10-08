`default_nettype none
`timescale 1ns / 1ps

// nlc_lossy: mode 1 (bior2.2 DWT + quantisation + static rANS), bit-exact
// against LossyCodec in model/nlc/lossy.py (docstring = format) with the
// tables in model/nlc/lossy_tables.json (= nlc_lossy_rom.svh).
//
//   smp ─► 3 x nlc_lift53 ─► quantise / a3 delta ─► per-channel FIFO
//            (state RAM)                              │ one pop per channel per sample
//                                                     ▼
//                        bytes ◄─ serialiser + header ◄─ nlc_rans (II=1)
//
// Samples arrive at most one per clock (the clock can be the ADC slot rate),
// frame-major, never stalled. Every channel is at the same block position, so
// the schedule (which levels fire, how many symbols are pushed and popped) is
// global; only data lives per channel.
//
// FIFO: a sample pushes 0..4 symbols (d1, d2, d3, delta a3) and every sample
// pops one per channel once the FIFO is non-empty, so the coder sees one
// symbol per input sample, in round-robin channel order. Peak occupancy is 7
// (nlc.lossy.fifo_profile); depth 8 leaves one entry for the coder to lag by
// up to a frame (packet flush). A lag of more than a frame sets `overflow`.
//
// Requirements: frames are long enough to absorb the packet flush (about
// coder depth + 3 * N_SEL * SB / 2 cycles; depth 2, or up to Q_W + 3 with DIV_REG), e.g. a 256-slot mux at one slot per clock.
// enable = 0 clears everything; the next packet is seq 0.
module nlc_lossy #(
    parameter int N_SEL = 8,
    parameter int SEL_W = 3,
    parameter int BPP_W = 2,      // 2^BPP_W blocks of 64 frames per packet
    parameter int   DIV_REG = 0      // coder divider registers (11-bit mask), see nlc_rans
) (
    input  logic             clk,
    input  logic             rst_n,
    input  logic             enable,
    input  logic [SEL_W:0]   n_sel,

    input  logic             smp_valid,
    input  logic [9:0]       smp_data,
    input  logic [SEL_W-1:0] smp_ch,
    input  logic             smp_last,     // channel n_sel-1 of a frame

    output logic             m_valid,
    input  logic             m_ready,
    output logic [7:0]       m_data,
    output logic             m_last,
    output logic             overflow
);
  // fixed by LossyConfig: adc_bits 10, block 64, levels 3, shifts a 1 / d (3, 2, 2),
  // s_max 31, esc_bytes 2, prob_bits 12, lsh 2
  localparam int QW     = 13;            // FIFO entry: widest value (a3 delta)
  localparam int QDEPTH = 8;
  localparam int S_MAX  = 31;
  localparam int ESC    = 2 * S_MAX + 1;

  logic clr_n;
  assign clr_n = rst_n && enable;

  // ---------------------------------------------------------------------------
  // Global schedule state
  // ---------------------------------------------------------------------------
  logic [5:0] n;                         // frame index in the block
  logic [2:0] wbase, rbase;              // FIFO write / read slot (same for all channels)
  logic [3:0] occ;                       // FIFO occupancy at the start of this frame
  logic [SEL_W+1:0] pending;             // pops owed to the coder
  logic [SEL_W-1:0] rr;                  // next channel to pop
  logic [5+BPP_W:0] j;                   // pop round within the packet

  // ---------------------------------------------------------------------------
  // Per-channel state (no reset: every register is written before it is read).
  // Clock-gated registers (nlc_greg): a channel's fields and FIFO slots only see
  // a clock edge when they are written, i.e. on its own slot. Packed arrays,
  // one element per channel / FIFO entry, driven by the nlc_greg instances.
  // ---------------------------------------------------------------------------
  logic [N_SEL-1:0][9:0]  e1, o1;
  logic [N_SEL-1:0][10:0] dp1;
  logic [N_SEL-1:0][10:0] e2, o2;
  logic [N_SEL-1:0][11:0] dp2;
  logic [N_SEL-1:0][11:0] e3, o3;
  logic [N_SEL-1:0][12:0] dp3;
  logic [N_SEL-1:0][11:0] qa_prev;
  logic [N_SEL*QDEPTH-1:0][QW-1:0] fifo;           // {channel, slot}
  logic [1:0]           fctx [0:QDEPTH-1];         // context per slot: global

  // ---------------------------------------------------------------------------
  // Lifting: level 1 on the centred sample, level l+1 on level l's a
  // ---------------------------------------------------------------------------
  logic signed [9:0] x0;
  assign x0 = {~smp_data[9], smp_data[8:0]};       // x - 512

  logic              pv1, pv2, pv3;
  logic [4:0]        p1;
  logic [3:0]        p2;
  logic [2:0]        p3;
  logic signed [10:0] a1, d1;
  logic signed [11:0] a2, d2;
  logic signed [12:0] a3, d3;
  logic e1_we, o1_we, dp1_we, e2_we, o2_we, dp2_we, e3_we, o3_we, dp3_we;

  nlc_lift53 #(.W(10), .N_W(6)) u_l1 (
      .v(smp_valid), .idx(n), .x(x0),
      .e(e1[smp_ch]), .o(o1[smp_ch]), .dp(dp1[smp_ch]),
      .pv(pv1), .p(p1), .a(a1), .d(d1), .e_we(e1_we), .o_we(o1_we), .dp_we(dp1_we));
  nlc_lift53 #(.W(11), .N_W(5)) u_l2 (
      .v(pv1), .idx(p1), .x(a1),
      .e(e2[smp_ch]), .o(o2[smp_ch]), .dp(dp2[smp_ch]),
      .pv(pv2), .p(p2), .a(a2), .d(d2), .e_we(e2_we), .o_we(o2_we), .dp_we(dp2_we));
  nlc_lift53 #(.W(12), .N_W(4)) u_l3 (
      .v(pv2), .idx(p2), .x(a2),
      .e(e3[smp_ch]), .o(o3[smp_ch]), .dp(dp3[smp_ch]),
      .pv(pv3), .p(p3), .a(a3), .d(d3), .e_we(e3_we), .o_we(o3_we), .dp_we(dp3_we));

  // sign(c) * (|c| >> sh). Two's complement handled through the MSB, no signed
  // casts or comparisons: Yosys and Icarus disagreed on those (see nlc_lift53).
  function automatic [QW-1:0] quant(input [QW-1:0] c, input integer sh);
    reg [QW-1:0] m;
    begin
      m = c[QW-1] ? ~c + 1'b1 : c;
      m = m >> sh;
      quant = c[QW-1] ? ~m + 1'b1 : m;
    end
  endfunction

  logic [QW-1:0] q1, q2, q3, qa3, da3;
  assign q1  = quant({{2{d1[10]}}, d1}, 3);
  assign q2  = quant({d2[11], d2}, 2);
  assign q3  = quant(d3, 2);
  assign qa3 = quant(a3, 1);
  logic [11:0] qa_p;                     // this channel's previous quantised a3
  assign qa_p = qa_prev[smp_ch];
  assign da3 = qa3 - ((p3 == '0) ? '0 : {qa_p[11], qa_p});

  // pushes this sample: d1 | d1 d2 | d1 d2 d3 a3   (always a prefix)
  logic [2:0] pushes;
  logic       pop_due;
  assign pushes  = {pv3, pv2 & ~pv3, pv1 & ~pv2};    // 0, 1, 2 or 4
  assign pop_due = (occ + 4'(pushes)) != '0;

  // ---------------------------------------------------------------------------
  // Per-channel state writes: one clock gate per (channel, field) and per
  // (channel, FIFO slot). A sample writes FIFO slots wbase .. wbase+3 with
  // d1, d2, d3, delta a3 (a prefix: pv3 => pv2 => pv1).
  // ---------------------------------------------------------------------------
  // FIFO slot s gets push (s - wbase): the same for every channel
  logic [QDEPTH-1:0]         slot_we;
  logic [QDEPTH-1:0][QW-1:0] slot_d;
  always_comb begin
    for (int s = 0; s < QDEPTH; s++) begin
      case (3'(s) - wbase)
        3'd0:    begin slot_we[s] = pv1; slot_d[s] = q1;  end
        3'd1:    begin slot_we[s] = pv2; slot_d[s] = q2;  end
        3'd2:    begin slot_we[s] = pv3; slot_d[s] = q3;  end
        3'd3:    begin slot_we[s] = pv3; slot_d[s] = da3; end
        default: begin slot_we[s] = 1'b0; slot_d[s] = da3; end
      endcase
    end
  end

  // Parent clock gates: a block's registers (and its child gates) only see a
  // clock edge when the block has work. Control registers clear asynchronously
  // on clr_n, so a disabled core (enable = 0) sees no clock edge at all (C-PWR-2).
  logic clk_s, clk_i;                     // sample (8 of 256 clocks), issuer
  logic r_fire;
  nlc_icg u_cg_s (.clk(clk), .en(smp_valid),           .gclk(clk_s));
  nlc_icg u_cg_i (.clk(clk), .en(smp_valid || r_fire), .gclk(clk_i));

  genvar gc, gs;
  generate
    for (gc = 0; gc < N_SEL; gc++) begin : g_ch
      logic wr;                          // this channel's sample, outside clear
      assign wr = clr_n && smp_valid && smp_ch == SEL_W'(gc);
      nlc_greg #(.W(10)) u_e1  (.clk(clk_s), .en(wr && e1_we),  .d(x0),        .q(e1[gc]));
      nlc_greg #(.W(10)) u_o1  (.clk(clk_s), .en(wr && o1_we),  .d(x0),        .q(o1[gc]));
      nlc_greg #(.W(11)) u_dp1 (.clk(clk_s), .en(wr && dp1_we), .d(d1),        .q(dp1[gc]));
      nlc_greg #(.W(11)) u_e2  (.clk(clk_s), .en(wr && e2_we),  .d(a1),        .q(e2[gc]));
      nlc_greg #(.W(11)) u_o2  (.clk(clk_s), .en(wr && o2_we),  .d(a1),        .q(o2[gc]));
      nlc_greg #(.W(12)) u_dp2 (.clk(clk_s), .en(wr && dp2_we), .d(d2),        .q(dp2[gc]));
      nlc_greg #(.W(12)) u_e3  (.clk(clk_s), .en(wr && e3_we),  .d(a2),        .q(e3[gc]));
      nlc_greg #(.W(12)) u_o3  (.clk(clk_s), .en(wr && o3_we),  .d(a2),        .q(o3[gc]));
      nlc_greg #(.W(13)) u_dp3 (.clk(clk_s), .en(wr && dp3_we), .d(d3),        .q(dp3[gc]));
      nlc_greg #(.W(12)) u_qa  (.clk(clk_s), .en(wr && pv3),    .d(qa3[11:0]), .q(qa_prev[gc]));
      for (gs = 0; gs < QDEPTH; gs++) begin : g_slot
        nlc_greg #(.W(QW)) u_q (.clk(clk_s), .en(wr && slot_we[gs]), .d(slot_d[gs]),
                                .q(fifo[gc * QDEPTH + gs]));
      end
    end
  endgenerate

  // ---------------------------------------------------------------------------
  // Issuer: pop channel rr's FIFO head into the coder
  // ---------------------------------------------------------------------------
  logic [QW-1:0] hv, hmag;
  logic [1:0]    hctx;
  logic          is_esc;
  logic [5:0]    hsym;
  logic [15:0]   hraw;
  logic          r_valid, r_ready, r_last;
  assign hv     = fifo[{rr, rbase}];
  assign hctx   = fctx[rbase];
  assign hmag   = hv[QW-1] ? ~hv + 1'b1 : hv;                 // |v|
  assign is_esc = hmag > QW'(S_MAX);
  assign hsym   = is_esc ? 6'(ESC) : 6'(hv + QW'(S_MAX));      // v + s_max (mod 64)
  assign hraw   = 16'({hv, 1'b0} ^ {(QW+1){hv[QW-1]}});    // zigzag
  assign r_valid = pending != '0;
  assign r_last  = &j && (rr == SEL_W'(n_sel - 1'b1));
  assign r_fire  = r_valid && r_ready;

  // ---------------------------------------------------------------------------
  // Sequential: front end schedule (clk_s), issuer (clk_i), overflow (clk)
  // ---------------------------------------------------------------------------
  always_ff @(posedge clk_s or negedge clr_n) begin
    if (!clr_n) begin
      n     <= '0;
      wbase <= '0;
      occ   <= '0;
    end else begin                        // smp_valid: per-channel state is in g_ch
      if (pv1) fctx[wbase]        <= 2'd1;
      if (pv2) fctx[wbase + 3'd1] <= 2'd2;
      if (pv3) begin
        fctx[wbase + 3'd2] <= 2'd3;
        fctx[wbase + 3'd3] <= 2'd0;
      end
      if (smp_last) begin
        n     <= n + 1'b1;
        wbase <= wbase + pushes;
        occ   <= occ + 4'(pushes) - 4'(pop_due);
      end
    end
  end

  always_ff @(posedge clk_i or negedge clr_n) begin
    if (!clr_n) begin
      rbase   <= '0;
      pending <= '0;
      rr      <= '0;
      j       <= '0;
    end else begin
      pending <= pending + (SEL_W+2)'(smp_valid && pop_due) - (SEL_W+2)'(r_fire);
      if (r_fire) begin
        if (rr == SEL_W'(n_sel - 1'b1)) begin
          rr    <= '0;
          rbase <= rbase + 1'b1;
          j     <= j + 1'b1;
        end else begin
          rr <= rr + 1'b1;
        end
      end
    end
  end

  always_ff @(posedge clk or negedge clr_n) begin   // sticky; checked every cycle
    if (!clr_n) overflow <= 1'b0;
    else if (pending > (SEL_W+2)'(n_sel)) overflow <= 1'b1;
  end

  // ---------------------------------------------------------------------------
  // Coder
  // ---------------------------------------------------------------------------
  logic        c_valid, c_ready, c_last;
  logic [31:0] c_data;
  logic [3:0]  c_keep;

  nlc_rans #(.N_CH(N_SEL), .CH_W(SEL_W), .DIV_REG(11'(DIV_REG))) u_rans (
      .clk(clk), .rst_n(clr_n),
      .s_tvalid(r_valid), .s_tready(r_ready), .s_tdata(hsym), .s_tctx(hctx),
      .s_tchan(rr), .s_traw_v(is_esc), .s_traw(hraw), .s_tlast(r_last),
      .m_tvalid(c_valid), .m_tready(c_ready), .m_tdata(c_data), .m_tkeep(c_keep),
      .m_tlast(c_last));

  // ---------------------------------------------------------------------------
  // Serialiser: header byte {mode = 1, seq}, then the coder's bytes
  // ---------------------------------------------------------------------------
  logic [31:0] sbuf;
  logic [2:0]  scnt;
  logic        slast, need_hdr;
  logic [5:0]  seq;

  assign c_ready = scnt == '0;
  assign m_valid = scnt != '0;
  assign m_data  = need_hdr ? {2'b01, seq} : sbuf[7:0];
  assign m_last  = !need_hdr && scnt == 3'd1 && slast;

  logic c_take, s_shift, clk_o;
  assign c_take  = c_valid && c_ready;
  assign s_shift = m_valid && m_ready && !need_hdr;
  nlc_icg u_cg_o (.clk(clk), .en(c_take || (m_valid && m_ready)), .gclk(clk_o));
  nlc_greg #(.W(32)) u_sbuf (.clk(clk_o), .en(clr_n && (c_take || s_shift)),
                             .d(c_take ? c_data : sbuf >> 8), .q(sbuf));

  always_ff @(posedge clk_o or negedge clr_n) begin
    if (!clr_n) begin
      scnt     <= '0;
      need_hdr <= 1'b1;
      seq      <= '0;
    end else if (c_valid && c_ready) begin
      scnt  <= c_keep[3] ? 3'd4 : c_keep[2] ? 3'd3 : c_keep[1] ? 3'd2 : 3'd1;
      slast <= c_last;
    end else if (m_valid && m_ready) begin
      if (need_hdr) begin
        need_hdr <= 1'b0;
      end else begin
        scnt <= scnt - 1'b1;
        if (m_last) begin
          need_hdr <= 1'b1;
          seq      <= seq + 1'b1;
        end
      end
    end
  end

endmodule
