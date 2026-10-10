`default_nettype none
`timescale 1ns / 1ps

// nlc_lossy: mode 1 (bior2.2 DWT + quantisation + static rANS), bit-exact
// against LossyCodec in model/nlc/lossy.py (docstring = format) with the
// tables in model/nlc/lossy_tables.json (= nlc_lossy_rom.svh).
//
//   smp ─► per-channel sample ─► issuer: 1 x nlc_lift53, one level per clock ─► quantise /
//          register (state RAM)     a3 delta ─► nlc_rans (II=1) ─► serialiser + header ─► bytes
//
// Samples arrive at most one per clock (the clock can be the ADC slot rate),
// frame-major, never stalled. Every channel is at the same block position, so
// the schedule (which levels fire, how many symbols are pushed and popped) is
// global; only data lives per channel.
//
// Bursts: a sample yields 0, 1, 2 or 4 symbols (d1, d2, d3, delta a3; the position
// gives the context). The issuer takes them as soon as the sample is there, frame by
// frame, channel by channel (nlc.lossy.coding_order), and lifts the sample one level
// per issued symbol (shared lifter), so only the sample is buffered per channel: at
// most 4 x N_SEL symbols per frame against a frame of clocks, so a burst is coded long
// before the channel's next sample. The coder may lag by up to a frame (packet flush);
// more than a frame sets `overflow` (the channel's next sample would overwrite the one
// not yet lifted). The output has no back-pressure (D8): one byte per clock while
// m_valid, and the consumer takes every byte.
//
// Requirements: frames are long enough to absorb the packet flush (about
// 3 * N_SEL * SB / 2 cycles) and a 4-symbol burst on every channel in two frames in a row
// (block positions 62, 63): 4 * N_SEL symbols at NC = ceil(10 / DIV_K) clocks each within
// one frame. The spec rate is II = 1, 256-slot = 256-clock frames (D9, C-IF-9): DIV_K = 5
// (NC = 2) leaves +138 clocks of slack for any data, bounded by the 1 byte/clock output,
// not by the divider (scripts/proofs/output_bound.py --d8). The TT pin path's 64-clock
// test frames are below spec: worst-case data can abort there. The issuer holds its symbol (and the state it
// was lifted from: writes happen only when the coder takes it) for the NC clocks.
// enable = 0 clears everything; the next packet is seq 0.
//
// Abort (D5/D6): when the coder falls more than a frame behind (a burst would be
// overwritten; a safety net: with no back-pressure (D8) it cannot happen at the real
// frame rate) or a frame was short (s_frame early), the packet in
// flight is dropped: issuer, coder and serialiser are cleared, samples are ignored but
// frames are still counted (smp_tick), and output resumes at the next packet boundary;
// its header carries that packet's seq, so the
// gap names the lost packets. abort_req tells the encoder to end a partly sent packet
// with the abort token. Blocks and rANS states restart at every packet boundary, so
// nothing else needs clearing.
module nlc_lossy #(
    parameter int N_SEL = 8,
    parameter int SEL_W = 3,
    parameter int BPP_W = 2,      // 2^BPP_W blocks of 64 frames per packet
    parameter int DIV_K = 5,          // coder: quotient bits per clock (>= 10: one symbol/clock), see nlc_rans
    parameter bit LATCH_ROWS = 1'b1   // per-channel storage in latch rows (nlc_lreg), else flops
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

    output logic             m_valid,      // one byte per clock, no back-pressure (D8)
    output logic [7:0]       m_data,
    output logic             m_last,
    output logic             overflow,     // sticky: a packet was aborted (coder a frame behind)
    output logic             abort_req     // packet in flight dropped (one clock)
);
  // fixed by LossyConfig: adc_bits 10, block 64, levels 3, shifts a 1 / d (3, 2, 2),
  // s_max 31, esc_bytes 2, prob_bits 12, lsh 2
  localparam int QW     = 13;            // issued value: widest (a3 delta)
  localparam int QDEPTH = 4;              // symbols per burst (d1, d2, d3, delta a3)
  localparam int LEVELS = 3;              // wavelet levels, one shared lifter (u_lift)
  // Value bounds of the lifting on 10-bit samples (|d1| <= 1023, |d2| <= 2046,
  // |d3| <= 4092, |a3| <= 4092): the state registers are as narrow as these allow.
  localparam int S_MAX  = 31;
  localparam int ESC    = 2 * S_MAX + 1;

  // Async clear built from logic, glitch-free with a synchronous release: rst_n is the TT
  // top's synchronised reset (project.v) and enable a flop that rst_n clears, so clr_n only
  // changes once per event: rst_n falling (enable falls after it: no 0-1-0), rst_n rising
  // (enable is 0 then: no change), enable changing on a clock edge (a config write). The
  // rANS clear clr_n && !skip likewise: skip changes on clk_l edges, and its own async clear
  // follows clr_n falling, when the AND is already 0. STA checks recovery / removal of both.
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
  // Clock-gated registers (nlc_greg): a channel's fields only see a clock edge
  // when they are written. Packed arrays, one element per channel.
  // ---------------------------------------------------------------------------
  logic [N_SEL-1:0][9:0]  sx;                      // the sample, until the issuer lifts it
  logic [N_SEL-1:0][9:0]  e1, o1;
  logic [N_SEL-1:0][10:0] dp1;
  logic [N_SEL-1:0][10:0] e2, o2;
  logic [N_SEL-1:0][11:0] dp2;
  logic [N_SEL-1:0][11:0] e3, o3;
  logic [N_SEL-1:0][12:0] dp3;
  logic [N_SEL-1:0][11:0] qa_prev;

  logic signed [9:0] x0;
  assign x0 = {~smp_data[9], smp_data[8:0]};       // x - 512

  // Symbols a sample at block position f pushes: d1 | d1 d2 | d1 d2 d3 a3 (a
  // prefix). The pair-complete rule of each level; nlc.lossy.push_schedule.
  function automatic [2:0] burst_len(input [5:0] pos);
    logic [4:0] i1;
    logic [3:0] i2;
    logic       c1, c2, c3;
    begin
      c1 = pos[0] ? &pos : pos != '0;
      i1 = pos[0] ? pos[5:1] : pos[5:1] - 1'b1;
      c2 = c1 && (i1[0] ? &i1 : i1 != '0);
      i2 = i1[0] ? i1[4:1] : i1[4:1] - 1'b1;
      c3 = c2 && (i2[0] ? &i2 : i2 != '0);
      burst_len = c3 ? 3'd4 : c2 ? 3'd2 : c1 ? 3'd1 : 3'd0;
    end
  endfunction

  logic [2:0] pushes;                    // this sample's burst size (test/env monitor)
  assign pushes = smp_valid ? burst_len(n_eff) : 3'd0;

  // ---------------------------------------------------------------------------
  // Parent clock gates: a block's registers (and its child gates) only see a
  // clock edge when the block has work. Control registers clear asynchronously
  // on clr_n, so a disabled core (enable = 0) sees no clock edge at all (C-PWR-2).
  // ---------------------------------------------------------------------------
  logic clk_l, clk_s, clk_i;              // lossy core, sample (8 of 256 clocks), issuer
  logic r_fire, c_active;
  // grandparent: the block gates below only see the clock while something is happening
  logic iss_busy;
  assign iss_busy = pending != '0;
  nlc_icg u_cg_l (.clk(clk), .en(smp_valid || smp_tick || iss_busy || c_active || abort_now ||
                                 m_valid), .gclk(clk_l));
  nlc_icg u_cg_s (.clk(clk_l), .en(smp_valid || smp_tick), .gclk(clk_s));
  nlc_icg u_cg_i (.clk(clk_l), .en(smp_valid || iss_busy || abort_now), .gclk(clk_i));

  // ---------------------------------------------------------------------------
  // Issuer + wavelet: the sample of channel rr in frame j is lifted one level per
  // clock, in the clock its symbol is issued (one shared nlc_lift53):
  //   kk = 0: level 1 on the stored sample -> d1 issued, a1 -> xr
  //   kk = 1: level 2 on a1               -> d2 issued, a2 -> xr
  //   kk = 2: level 3 on a2               -> d3 issued, delta a3 -> xr
  //   kk = 3: delta a3 issued from xr
  // A level whose pair is not complete only stores its input (no lift): level 1 in
  // the empty-burst frames (one clock per channel, nothing issued), level 2 / 3 in
  // the clock that computed that input (a1 / a2 straight from the lifter). The issue
  // order and cycle timing are those of the former burst buffer (bursts written at
  // the sample, read from the next clock on); the only per-channel buffer left is the
  // sample, which the issuer reads before that channel's next sample (pending <= n_sel,
  // else abort; blocks restart at every packet, so an abort leaves no stale state read).
  // ---------------------------------------------------------------------------
  logic [QW-1:0] hv, hmag;
  logic [1:0]    hctx;
  logic          is_esc;
  logic [5:0]    hsym;
  logic [15:0]   hraw;
  logic          r_valid, r_ready, r_last;
  logic [2:0]    nb;                     // burst size of frame j (all channels)
  logic          b_end, unit_done;       // last symbol of the burst / channel sample done
  assign b_end  = 3'(kk) + 1'b1 == nb;

  // pair schedule of frame j (global): pair complete at each level, input / pair indices
  logic [5:0] f;
  logic [4:0] f1;
  logic [3:0] f2;
  logic [2:0] f3;
  logic       pv1, pv2, pv3;
  assign f   = j[5:0];
  assign pv1 = f[0] ? &f : f != '0;
  assign f1  = f[0] ? f[5:1] : f[5:1] - 1'b1;
  assign pv2 = pv1 && (f1[0] ? &f1 : f1 != '0);
  assign f2  = f1[0] ? f1[4:1] : f1[4:1] - 1'b1;
  assign pv3 = pv2 && (f2[0] ? &f2 : f2 != '0);
  assign f3  = f2[0] ? f2[3:1] : f2[3:1] - 1'b1;
  assign nb  = pv3 ? 3'd4 : pv2 ? 3'd2 : pv1 ? 3'd1 : 3'd0;

  // channel rr's state, then the level's operands (sign-extended to the lifter's 12 bits)
  logic [9:0]  s_r, e1_r, o1_r;
  logic [10:0] dp1_r, e2_r, o2_r;
  logic [11:0] dp2_r, e3_r, o3_r, qa_p;
  logic [12:0] dp3_r;
  assign s_r   = sx[rr];
  assign e1_r  = e1[rr];
  assign o1_r  = o1[rr];
  assign dp1_r = dp1[rr];
  assign e2_r  = e2[rr];
  assign o2_r  = o2[rr];
  assign dp2_r = dp2[rr];
  assign e3_r  = e3[rr];
  assign o3_r  = o3[rr];
  assign dp3_r = dp3[rr];
  assign qa_p  = qa_prev[rr];

  logic [12:0] xr;                       // a1 / a2 between levels, then delta a3
  logic [11:0] lx, le, lo;
  logic [12:0] ldp;
  logic        lodd, lfirst;
  always_comb begin
    case (kk)
      2'd0: begin
        lx = {{2{s_r[9]}}, s_r};     le = {{2{e1_r[9]}}, e1_r};   lo = {{2{o1_r[9]}}, o1_r};
        ldp = {{2{dp1_r[10]}}, dp1_r}; lodd = f[0];  lfirst = f1 == '0;
      end
      2'd1: begin
        lx = xr[11:0];               le = {e2_r[10], e2_r};       lo = {o2_r[10], o2_r};
        ldp = {dp2_r[11], dp2_r};    lodd = f1[0]; lfirst = f2 == '0;
      end
      default: begin
        lx = xr[11:0];               le = e3_r;                   lo = o3_r;
        ldp = dp3_r;                 lodd = f2[0]; lfirst = f3 == '0;
      end
    endcase
  end

  logic [12:0] la, ld;
  nlc_lift53 #(.W(12)) u_lift (
      .odd(lodd), .first(lfirst), .x(lx), .e(le), .o(lo), .dp(ldp), .a(la), .d(ld));

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
  assign q1  = quant(ld, 3);             // ld = d1 / d2 / d3 at kk = 0 / 1 / 2
  assign q2  = quant(ld, 2);
  assign q3  = quant(ld, 2);
  assign qa3 = quant(la, 1);             // la = a3 at kk = 2
  assign da3 = qa3 - ((f3 == '0) ? '0 : {qa_p[11], qa_p});

  always_comb begin
    case (kk)
      2'd0:    hv = q1;
      2'd1:    hv = q2;
      2'd2:    hv = q3;
      default: hv = xr;
    endcase
  end

  // one issuer step done this clock (a symbol taken, or an empty-burst channel walked)
  logic st, st0, st1, st2;
  assign st  = iss_busy && (nb == '0 || r_fire);
  assign st0 = st && kk == 2'd0;
  assign st1 = st && kk == 2'd1;
  assign st2 = st && kk == 2'd2;
  logic l2_in, l3_in;                    // level 2 / 3 stores its input this clock
  assign l2_in = (st0 && nb == 3'd1) || st1;
  assign l3_in = (st1 && nb == 3'd2) || st2;
  logic [10:0] e2_d;
  logic [11:0] e3_d;
  assign e2_d = kk == 2'd0 ? la[10:0] : lx[10:0];  // a1 from the lifter / from xr
  assign e3_d = kk == 2'd1 ? la[11:0] : lx[11:0];  // a2 from the lifter / from xr

  nlc_greg #(.W(13)) u_x (.clk(clk_i), .en((st0 && pv2) || (st1 && pv3) || st2),
                          .d(st2 ? da3 : la), .q(xr));

  // Per-channel rows (nlc_rreg): latch rows when LATCH_ROWS. A latch row is open for
  // the high phase after its write edge, so its data must come from flops loaded at that
  // edge: the lifting reads and rewrites channel rr's state in the same step (a latch fed
  // by the lifter would loop through its own output), and the sample comes from the slot
  // selector's register, which can take the next channel's sample at that edge. Staging
  // registers, shared by all channels (one channel is written per step):
  //   sg_xq: level-1 input (e1 / o1, kk = 0) or quantised a3 (qa_prev, kk = 2)
  //   sg_e2 / sg_e3: level-2 / 3 input (e2 / o2, e3 / o3; both can be written at kk = 1)
  //   sg_dp: the level's detail (dp1 / dp2 / dp3)
  //   sg_sx: the sample (sx, loaded with every sample taken)
  // Every row then reads and updates exactly like the flops it replaces (new value from
  // the same edge); a row is never written and read-modified in the same step.
  logic [11:0] sg_xq, w_xq;
  logic [10:0] sg_e2;
  logic [11:0] sg_e3;
  logic [12:0] sg_dp;
  logic [9:0]  sg_sx;
  assign w_xq = kk == 2'd2 ? qa3[11:0] : {2'b00, lx[9:0]};
  generate
    if (LATCH_ROWS) begin : g_sg
      nlc_greg #(.W(12 + 11 + 12 + 13)) u_sg (.clk(clk_i), .en(st),
          .d({w_xq, e2_d, e3_d, ld}), .q({sg_xq, sg_e2, sg_e3, sg_dp}));
      nlc_greg #(.W(10)) u_sgx (.clk(clk_s), .en(clr_n && run && smp_valid), .d(x0), .q(sg_sx));
    end else begin : g_nosg
      assign {sg_xq, sg_e2, sg_e3, sg_dp, sg_sx} = {w_xq, e2_d, e3_d, ld, x0};
    end
  endgenerate

  genvar gc;
  generate
    for (gc = 0; gc < N_SEL; gc++) begin : g_ch
      logic wr, iw;                      // this channel's sample (outside clear) / issue step
      assign wr = clr_n && run && smp_valid && smp_ch == SEL_W'(gc);
      assign iw = rr == SEL_W'(gc);
      nlc_rreg #(.W(10), .LATCH(LATCH_ROWS)) u_sx  (.clk(clk_s), .en(wr),                    .d(sg_sx),         .q(sx[gc]));
      nlc_rreg #(.W(10), .LATCH(LATCH_ROWS)) u_e1  (.clk(clk_i), .en(iw && st0 && !f[0]),    .d(sg_xq[9:0]),    .q(e1[gc]));
      nlc_rreg #(.W(10), .LATCH(LATCH_ROWS)) u_o1  (.clk(clk_i), .en(iw && st0 && f[0]),     .d(sg_xq[9:0]),    .q(o1[gc]));
      nlc_rreg #(.W(11), .LATCH(LATCH_ROWS)) u_dp1 (.clk(clk_i), .en(iw && st0 && pv1),      .d(sg_dp[10:0]),   .q(dp1[gc]));
      nlc_rreg #(.W(11), .LATCH(LATCH_ROWS)) u_e2  (.clk(clk_i), .en(iw && l2_in && !f1[0]), .d(sg_e2),         .q(e2[gc]));
      nlc_rreg #(.W(11), .LATCH(LATCH_ROWS)) u_o2  (.clk(clk_i), .en(iw && l2_in && f1[0]),  .d(sg_e2),         .q(o2[gc]));
      nlc_rreg #(.W(12), .LATCH(LATCH_ROWS)) u_dp2 (.clk(clk_i), .en(iw && st1),             .d(sg_dp[11:0]),   .q(dp2[gc]));
      nlc_rreg #(.W(12), .LATCH(LATCH_ROWS)) u_e3  (.clk(clk_i), .en(iw && l3_in && !f2[0]), .d(sg_e3),         .q(e3[gc]));
      nlc_rreg #(.W(12), .LATCH(LATCH_ROWS)) u_o3  (.clk(clk_i), .en(iw && l3_in && f2[0]),  .d(sg_e3),         .q(o3[gc]));
      nlc_rreg #(.W(13), .LATCH(LATCH_ROWS)) u_dp3 (.clk(clk_i), .en(iw && st2),             .d(sg_dp),         .q(dp3[gc]));
      nlc_rreg #(.W(12), .LATCH(LATCH_ROWS)) u_qa  (.clk(clk_i), .en(iw && st2),             .d(sg_xq),         .q(qa_prev[gc]));
    end
  endgenerate

  // A channel sample with an empty burst takes one clock and issues nothing; the
  // coder sees channel 0 meanwhile instead of toggling (operand isolation).
  logic [SEL_W-1:0] rr_iss;
  assign rr_iss = nb != '0 ? rr : '0;
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
  // next packet start (the token is out in the clock after the abort)
  assign abort_now  = clr_n && !skip && (pending > (SEL_W+2)'(n_sel) || (smp_tick && smp_short));
  assign resume_now = skip && pkt_start;
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

  always_ff @(posedge clk_i or negedge clr_n) begin // sticky: an abort, coder a frame behind
    if (!clr_n) overflow <= 1'b0;
    else if (pending > (SEL_W+2)'(n_sel)) overflow <= 1'b1;
  end

  // ---------------------------------------------------------------------------
  // Coder
  // ---------------------------------------------------------------------------
  logic        c_valid, c_ready, c_last;
  logic [31:0] c_data;
  logic [3:0]  c_keep;

  nlc_rans #(.N_CH(N_SEL), .CH_W(SEL_W), .DIV_K(DIV_K), .LATCH_ROWS(LATCH_ROWS)) u_rans (
      .clk(clk_l), .rst_n(clr_n && !skip), .active(c_active),
      .s_tvalid(r_valid), .s_tready(r_ready), .s_tdata(hsym), .s_tctx(hctx),
      .s_tchan(rr_iss), .s_traw_v(is_esc), .s_traw(hraw), .s_tlast(r_last),
      .m_tvalid(c_valid), .m_tready(c_ready), .m_tdata(c_data), .m_tkeep(c_keep),
      .m_tlast(c_last));

  // ---------------------------------------------------------------------------
  // Serialiser: header byte {mode = 1, seq}, then the bytes of the coder's output
  // word, read in place (no copy), one byte per clock; the word is taken (c_ready) with
  // its last byte, so the coder cannot overwrite it before it is out.
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
  // qualified by c_valid: w_end reads the unreset word register (X until the first word); it
  // reached the coder's clock gate and made m_valid X at gate level. Bit-identical (D8 11071e4).
  assign c_ready = c_valid && !need_hdr && w_end;

  logic clk_o;
  nlc_icg u_cg_o (.clk(clk_l), .en(m_valid || abort_now || resume_now),
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
