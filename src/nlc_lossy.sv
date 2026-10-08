`default_nettype none
`timescale 1ns / 1ps

// nlc_lossy: mode 1 (bior2.2 DWT + quantisation + static rANS), bit-exact
// against LossyCodec in model/nlc/lossy.py (docstring = format) with the
// tables in model/nlc/lossy_tables.json (= nlc_lossy_rom.svh).
//
//   smp ─► 3 x nlc_lift53 ─► quantise / a3 delta ─► per-channel burst buffer
//            (state RAM)                              │ drained eagerly, frame / channel order
//                                                     ▼
//                        bytes ◄─ serialiser + header ◄─ nlc_rans (II=1)
//
// Samples arrive at most one per clock (the clock can be the ADC slot rate),
// frame-major, never stalled. Every channel is at the same block position, so
// the schedule (which levels fire, how many symbols are pushed and popped) is
// global; only data lives per channel.
//
// Burst buffer: a sample pushes 0, 1, 2 or 4 symbols (d1, d2, d3, delta a3) into
// fixed slots 0..3 of its channel's buffer (the slot gives the context). The
// issuer drains every burst as soon as it is there, frame by frame, channel by
// channel (nlc.lossy.coding_order): at most 4 x N_SEL symbols per frame against
// a frame of clocks, so a burst is coded long before the channel's next sample.
// The coder may lag by up to a frame (packet flush, output back-pressure); more
// than a frame sets `overflow` (the channel's next burst would overwrite it).
//
// Requirements: frames are long enough to absorb the packet flush (about
// coder depth + 3 * N_SEL * SB / 2 cycles; depth 2, or up to Q_W + 3 with DIV_REG), e.g. a 256-slot mux at one slot per clock.
// enable = 0 clears everything; the next packet is seq 0.
//
// Abort (D5/D6): when the coder falls more than a frame behind (a burst would be
// overwritten: blocked output) or a frame was short (s_frame early), the packet in
// flight is dropped: issuer, coder and serialiser are cleared, samples are ignored but
// frames are still counted (smp_tick), and output resumes at the next packet boundary
// at which the output can take bytes again; its header carries that packet's seq, so the
// gap names the lost packets. abort_req tells the encoder to end a partly sent packet
// with the abort token. Blocks and rANS states restart at every packet boundary, so
// nothing else needs clearing.
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
    input  logic             smp_tick,     // a new frame started (s_frame; not the first)
    input  logic             smp_short,    // with smp_tick: the frame that ended was short

    output logic             m_valid,
    input  logic             m_ready,
    output logic [7:0]       m_data,
    output logic             m_last,
    output logic             overflow,     // sticky: a packet was aborted (blocked output)
    output logic             abort_req     // packet in flight dropped (one clock)
);
  // fixed by LossyConfig: adc_bits 10, block 64, levels 3, shifts a 1 / d (3, 2, 2),
  // s_max 31, esc_bytes 2, prob_bits 12, lsh 2
  localparam int QW     = 13;            // FIFO entry: widest value (a3 delta)
  localparam int QDEPTH = 4;              // burst buffer slots per channel
  // Slot widths: safe interval bounds of the lifting on 10-bit samples (|d1| <= 1023,
  // |d2| <= 2046, |d3| <= 4092, |a3| <= 4092): |q1| <= 127, |q2| <= 511, |q3| <= 1023,
  // |delta a3| <= 4092. Stored narrow, sign-extended on read.
  localparam int W0 = 8, W1 = 10, W2 = 11, W3 = QW;
  localparam int S_MAX  = 31;
  localparam int ESC    = 2 * S_MAX + 1;

  logic clr_n;
  assign clr_n = rst_n && enable;

  // ---------------------------------------------------------------------------
  // Global schedule state
  // ---------------------------------------------------------------------------
  logic [5:0] n;                         // frame index in the block (samples)
  logic [BPP_W-1:0] blk;                 // block in the packet (samples)
  logic [5:0] pk;                        // packet number (samples): seq after a resume
  logic [5:0] n_eff;                     // frame of this cycle's sample (smp_tick counts now)
  logic       skip, abort_now, resume_now, run;
  assign n_eff = n + 6'(smp_tick);
  logic [SEL_W+1:0] pending;             // channel samples whose burst is not issued yet
  logic [SEL_W-1:0] rr;                  // issuer: channel
  logic [1:0]       kk;                  // issuer: symbol in the channel's burst
  logic [5+BPP_W:0] j;                   // issuer: frame within the packet

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
  logic [N_SEL-1:0][W0-1:0] bq0;                   // burst buffers: slot 0..3 per channel
  logic [N_SEL-1:0][W1-1:0] bq1;
  logic [N_SEL-1:0][W2-1:0] bq2;
  logic [N_SEL-1:0][W3-1:0] bq3;

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
      .v(smp_valid), .idx(n_eff), .x(x0),
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

  // Symbols a sample at block position f pushes: d1 | d1 d2 | d1 d2 d3 a3 (a
  // prefix). The pair-complete rule of nlc_lift53 on each level; nlc.lossy.push_schedule.
  function automatic [2:0] burst_len(input [5:0] f);
    logic [4:0] f1;
    logic [3:0] f2;
    logic       v1, v2, v3;
    begin
      v1 = f[0] ? &f : f != '0;
      f1 = f[0] ? f[5:1] : f[5:1] - 1'b1;
      v2 = v1 && (f1[0] ? &f1 : f1 != '0);
      f2 = f1[0] ? f1[4:1] : f1[4:1] - 1'b1;
      v3 = v2 && (f2[0] ? &f2 : f2 != '0);
      burst_len = v3 ? 3'd4 : v2 ? 3'd2 : v1 ? 3'd1 : 3'd0;
    end
  endfunction

  logic [2:0] pushes;                    // this sample's burst size (test/env monitor)
  assign pushes = {pv3, pv2 & ~pv3, pv1 & ~pv2};

  // ---------------------------------------------------------------------------
  // Per-channel state writes: one clock gate per (channel, field) and per
  // (channel, burst slot). Slot k holds the k-th push: d1, d2, d3, delta a3.
  // ---------------------------------------------------------------------------

  // Parent clock gates: a block's registers (and its child gates) only see a
  // clock edge when the block has work. Control registers clear asynchronously
  // on clr_n, so a disabled core (enable = 0) sees no clock edge at all (C-PWR-2).
  logic clk_l, clk_s, clk_i;              // lossy core, sample (8 of 256 clocks), issuer
  logic r_fire, c_active;
  // grandparent: the block gates below only see the clock while something is happening
  logic iss_busy;
  assign iss_busy = pending != '0;
  nlc_icg u_cg_l (.clk(clk), .en(smp_valid || smp_tick || iss_busy || c_active || abort_now ||
                                 (m_valid && m_ready)), .gclk(clk_l));
  nlc_icg u_cg_s (.clk(clk_l), .en(smp_valid || smp_tick), .gclk(clk_s));
  nlc_icg u_cg_i (.clk(clk_l), .en(smp_valid || iss_busy || abort_now), .gclk(clk_i));

  genvar gc;
  generate
    for (gc = 0; gc < N_SEL; gc++) begin : g_ch
      logic wr;                          // this channel's sample, outside clear
      assign wr = clr_n && run && smp_valid && smp_ch == SEL_W'(gc);
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
      nlc_greg #(.W(W0)) u_b0 (.clk(clk_s), .en(wr && pv1), .d(q1[W0-1:0]),  .q(bq0[gc]));
      nlc_greg #(.W(W1)) u_b1 (.clk(clk_s), .en(wr && pv2), .d(q2[W1-1:0]),  .q(bq1[gc]));
      nlc_greg #(.W(W2)) u_b2 (.clk(clk_s), .en(wr && pv3), .d(q3[W2-1:0]),  .q(bq2[gc]));
      nlc_greg #(.W(W3)) u_b3 (.clk(clk_s), .en(wr && pv3), .d(da3[W3-1:0]), .q(bq3[gc]));
    end
  endgenerate

  // ---------------------------------------------------------------------------
  // Issuer: symbol kk of channel rr's burst in frame j into the coder. A channel
  // sample with an empty burst takes one clock and issues nothing.
  // ---------------------------------------------------------------------------
  logic [QW-1:0] hv, hmag;
  logic [1:0]    hctx;
  logic          is_esc;
  logic [5:0]    hsym;
  logic [15:0]   hraw;
  logic          r_valid, r_ready, r_last;
  logic [2:0]    nb;                     // burst size of frame j (all channels)
  logic          b_end, unit_done;       // last symbol of the burst / channel sample done
  assign nb     = burst_len(j[5:0]);
  assign b_end  = 3'(kk) + 1'b1 == nb;
  // Empty-burst frames (half of them) walk rr without issuing: the read muxes, ROM and
  // state read see channel 0 meanwhile instead of toggling (operand isolation).
  logic [SEL_W-1:0] rr_iss;
  logic [W0-1:0]    v0;
  logic [W1-1:0]    v1;
  logic [W2-1:0]    v2;
  logic [W3-1:0]    v3;
  assign rr_iss = nb != '0 ? rr : '0;
  assign v0 = bq0[rr_iss];
  assign v1 = bq1[rr_iss];
  assign v2 = bq2[rr_iss];
  assign v3 = bq3[rr_iss];
  always_comb begin
    case (kk)
      2'd0:    hv = {{(QW-W0){v0[W0-1]}}, v0};
      2'd1:    hv = {{(QW-W1){v1[W1-1]}}, v1};
      2'd2:    hv = {{(QW-W2){v2[W2-1]}}, v2};
      default: hv = v3;
    endcase
  end
  assign hctx   = kk == 2'd3 ? 2'd0 : kk + 1'b1;               // d1, d2, d3, a3
  assign hmag   = hv[QW-1] ? ~hv + 1'b1 : hv;                 // |v|
  assign is_esc = hmag > QW'(S_MAX);
  assign hsym   = is_esc ? 6'(ESC) : 6'(hv + QW'(S_MAX));      // v + s_max (mod 64)
  assign hraw   = 16'({hv, 1'b0} ^ {(QW+1){hv[QW-1]}});    // zigzag
  assign r_valid   = iss_busy && nb != '0;
  assign r_last    = &j && (rr == SEL_W'(n_sel - 1'b1)) && b_end;
  assign r_fire    = r_valid && r_ready;
  assign unit_done = iss_busy && (nb == '0 || (r_fire && b_end));

  // ---------------------------------------------------------------------------
  // Sequential: front end schedule (clk_s), issuer (clk_i), overflow (clk)
  // ---------------------------------------------------------------------------
  // frames are counted from s_frame (smp_tick), also while skipping
  logic pkt_start;                        // this tick starts frame 0 of a packet
  assign pkt_start = smp_tick && &n && &blk;
  always_ff @(posedge clk_s or negedge clr_n) begin
    if (!clr_n) begin
      n   <= '0;
      blk <= '0;
      pk  <= '0;
    end else if (smp_tick) begin
      n <= n + 1'b1;
      if (&n) begin
        blk <= blk + 1'b1;
        if (&blk) pk <= pk + 1'b1;
      end
    end
  end

  // abort: the coder is more than a frame behind, or a frame was short; resume at the
  // first packet start at which the output takes bytes again (the token is out first)
  assign abort_now  = clr_n && !skip && (pending > (SEL_W+2)'(n_sel) || (smp_tick && smp_short));
  assign resume_now = skip && pkt_start && m_ready;
  assign run        = !skip || resume_now;    // samples of this cycle are processed
  assign abort_req  = abort_now;
  always_ff @(posedge clk_l or negedge clr_n) begin
    if (!clr_n)         skip <= 1'b0;
    else if (abort_now) skip <= 1'b1;
    else if (resume_now) skip <= 1'b0;
  end

  always_ff @(posedge clk_i or negedge clr_n) begin
    if (!clr_n) begin
      pending <= '0;
      rr      <= '0;
      kk      <= '0;
      j       <= '0;
    end else if (abort_now || (skip && !resume_now)) begin  // dropped: restart at a packet
      pending <= '0;
      rr      <= '0;
      kk      <= '0;
      j       <= '0;
    end else begin
      pending <= pending + (SEL_W+2)'(smp_valid) - (SEL_W+2)'(unit_done);
      if (unit_done) begin
        kk <= '0;
        if (rr == SEL_W'(n_sel - 1'b1)) begin
          rr <= '0;
          j  <= j + 1'b1;
        end else begin
          rr <= rr + 1'b1;
        end
      end else if (r_fire) begin
        kk <= kk + 1'b1;
      end
    end
  end

  always_ff @(posedge clk_i or negedge clr_n) begin // sticky: an abort for blocked output
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
      .clk(clk_l), .rst_n(clr_n && !skip), .active(c_active),
      .s_tvalid(r_valid), .s_tready(r_ready), .s_tdata(hsym), .s_tctx(hctx),
      .s_tchan(rr_iss), .s_traw_v(is_esc), .s_traw(hraw), .s_tlast(r_last),
      .m_tvalid(c_valid), .m_tready(c_ready), .m_tdata(c_data), .m_tkeep(c_keep),
      .m_tlast(c_last));

  // ---------------------------------------------------------------------------
  // Serialiser: header byte {mode = 1, seq}, then the bytes of the coder's output
  // word, read in place (no copy); the word is taken (c_ready) with its last byte.
  // ---------------------------------------------------------------------------
  logic [1:0] bi;                         // byte of the coder's word being sent
  logic [1:0] b_last;                     // its last byte (keep is contiguous from bit 0)
  logic       w_end, need_hdr;
  logic [5:0] seq;

  assign b_last  = c_keep[3] ? 2'd3 : c_keep[2] ? 2'd2 : c_keep[1] ? 2'd1 : 2'd0;
  assign w_end   = bi == b_last;
  assign m_valid = c_valid && !skip;
  assign m_data  = need_hdr ? {2'b01, seq} : c_data[8 * bi +: 8];
  assign m_last  = !need_hdr && w_end && c_last;
  assign c_ready = m_ready && !need_hdr && w_end;

  logic clk_o;
  nlc_icg u_cg_o (.clk(clk_l), .en((m_valid && m_ready) || abort_now || resume_now),
                  .gclk(clk_o));

  always_ff @(posedge clk_o or negedge clr_n) begin
    if (!clr_n) begin
      bi       <= '0;
      need_hdr <= 1'b1;
      seq      <= '0;
    end else if (abort_now) begin         // the packet in flight is dropped
      bi       <= '0;
      need_hdr <= 1'b1;
    end else if (resume_now) begin        // first packet after an abort: its own seq
      seq <= pk + 1'b1;                   // resume is at a packet start: pk counts now
    end else if (need_hdr) begin          // a byte was taken
      need_hdr <= 1'b0;
    end else if (w_end) begin
      bi <= '0;
      if (c_last) begin
        need_hdr <= 1'b1;
        seq      <= seq + 1'b1;
      end
    end else begin
      bi <= bi + 1'b1;
    end
  end

endmodule
