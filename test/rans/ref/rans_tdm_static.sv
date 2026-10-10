`default_nettype none
`timescale 1ns / 1ps

// rans_tdm_static: II=1 pipelined, channel-interleaved rANS with loadable
// static tables (one per context, e.g. wavelet subband).
// Bit-exact against TdmRansStaticEncoder in model/nlc/rans_tdm.py (docstring = format).
//
//   accept ─► A: table lookup, state read ─► renorm (0-2 bytes out)
//          ─► D[0..Q_W-1]: one quotient bit per stage ─► WB: (q << PB) + r + c ─► state[ch]
//
// Table: N_CTX x N_SYM entries, entry {ctx, s} = cdf_ctx[s] (cdf_ctx[0] = 0,
// strictly increasing, below 2^PB; cdf_ctx[N_SYM] = 2^PB is implied). Every
// table sums to M = 2^PB, so L = 2^(PB+LSH) is a constant and q*M is a shift.
// Load it through tbl_we/tbl_addr/tbl_data while tbl_ready = 1 (no packet in
// progress); writes while tbl_ready = 0 are ignored. Reset contents: uniform.
//
// Hazard rule: as rans_tdm_adaptive. The same channel must not be issued
// again within PIPE_DEPTH pipeline advances (round-robin over N_CH >= PIPE_DEPTH).
module rans_tdm_static #(
    parameter int N_SYM = 64,
    parameter int SYM_W = 6,       // $clog2(N_SYM)
    parameter int N_CH  = 32,      // >= PIPE_DEPTH
    parameter int CH_W  = 5,       // $clog2(N_CH)
    parameter int N_CTX = 4,
    parameter int CTX_W = 2,       // $clog2(N_CTX)
    parameter int PB    = 12,      // every table sums to 2^PB
    parameter int LSH   = 11       // L = 2^PB << LSH
) (
    input  logic                   clk,
    input  logic                   rst_n,

    // table load port
    input  logic                   tbl_we,
    input  logic [CTX_W+SYM_W-1:0] tbl_addr,   // {ctx, symbol}
    input  logic [PB-1:0]          tbl_data,   // cdf value
    output logic                   tbl_ready,

    input  logic                   s_tvalid,
    output logic                   s_tready,
    input  logic [SYM_W-1:0]       s_tdata,
    input  logic [CTX_W-1:0]       s_tctx,
    input  logic [CH_W-1:0]        s_tchan,
    input  logic                   s_tlast,    // last symbol of the packet

    output logic                   m_tvalid,
    input  logic                   m_tready,
    output logic [15:0]            m_tdata,    // first byte in [7:0]
    output logic [1:0]             m_tkeep,    // 2'b01: one byte, 2'b11: two bytes
    output logic                   m_tlast
);

  localparam int F_W        = PB + 1;              // f_s, c_s, remainder
  localparam int Q_W        = LSH + 8;             // quotient bits: x < f_s << Q_W
  localparam int X_W        = Q_W + PB;            // state: x < 2^PB << Q_W
  localparam int PIPE_DEPTH = Q_W + 3;             // accept -> state write-back
  localparam int N_TBL      = N_CTX * N_SYM;

  // ---------------------------------------------------------------------------
  // Pipeline advance: everything moves unless the output register is stuck.
  // ---------------------------------------------------------------------------
  logic o_v;
  logic adv;
  assign adv = !(o_v && !m_tready);

  typedef enum logic [1:0] {RUN, DRAIN, FLUSH} phase_t;
  phase_t phase;
  logic   pkt_open;

  assign tbl_ready = !pkt_open;
  assign s_tready  = adv && (phase == RUN) && !tbl_we;
  logic fire;
  assign fire = s_tvalid && s_tready;

  // ---------------------------------------------------------------------------
  // Table memory (loadable "ROM")
  // ---------------------------------------------------------------------------
  logic [PB-1:0] tbl [0:N_TBL-1];

  logic [CTX_W+SYM_W-1:0] t_idx;
  logic [F_W-1:0]         in_c, in_next, in_f;
  assign t_idx   = {s_tctx, s_tdata};
  assign in_c    = F_W'(tbl[t_idx]);
  assign in_next = (s_tdata == SYM_W'(N_SYM - 1)) ? F_W'(1 << PB) : F_W'(tbl[t_idx + 1'b1]);
  assign in_f    = in_next - in_c;

  // ---------------------------------------------------------------------------
  // Per-channel state (context memory) and "not used yet this packet" flags.
  // ---------------------------------------------------------------------------
  localparam logic [X_W-1:0] L_INIT = X_W'(1) << (PB + LSH);

  logic [X_W-1:0]  st_mem [0:N_CH-1];
  logic [N_CH-1:0] fresh;
  logic [X_W-1:0]  in_x;
  assign in_x = fresh[s_tchan] ? L_INIT : st_mem[s_tchan];

  // ---------------------------------------------------------------------------
  // Stage A registers
  // ---------------------------------------------------------------------------
  logic            a_v;
  logic [X_W-1:0]  a_x;
  logic [F_W-1:0]  a_f, a_c;
  logic [CH_W-1:0] a_ch;

  // renormalisation (combinational from stage A): 0, 1 or 2 bytes
  logic [X_W:0]   thr;
  logic [1:0]     rn_k;
  logic [X_W-1:0] rn_x;
  assign thr = (X_W + 1)'(a_f) << Q_W;
  always_comb begin
    if ({1'b0, a_x} >= thr) begin
      if ({9'd0, a_x[X_W-1:8]} >= thr) begin
        rn_k = 2'd2;
        rn_x = a_x >> 16;
      end else begin
        rn_k = 2'd1;
        rn_x = a_x >> 8;
      end
    end else begin
      rn_k = 2'd0;
      rn_x = a_x;
    end
  end

  // ---------------------------------------------------------------------------
  // Divider pipeline: D[j] holds the partial result after j quotient bits.
  // ---------------------------------------------------------------------------
  logic            d_v   [0:Q_W];
  logic [F_W-1:0]  d_rem [0:Q_W];
  logic [Q_W-1:0]  d_low [0:Q_W];
  logic [Q_W-1:0]  d_q   [0:Q_W];
  logic [F_W-1:0]  d_f   [0:Q_W];
  logic [F_W-1:0]  d_c   [0:Q_W];
  logic [CH_W-1:0] d_ch  [0:Q_W];

  logic [F_W:0]    dv_t  [0:Q_W-1];
  logic            dv_ge [0:Q_W-1];
  always_comb begin
    for (int j = 0; j < Q_W; j++) begin
      dv_t[j]  = {d_rem[j], d_low[j][Q_W-1-j]};
      dv_ge[j] = dv_t[j] >= {1'b0, d_f[j]};
    end
  end

  // write-back: x' = (q << PB) + r + c   (M = 2^PB: no multiplier)
  logic [X_W-1:0] wb_x;
  assign wb_x = (X_W'(d_q[Q_W]) << PB) + X_W'(d_rem[Q_W]) + X_W'(d_c[Q_W]);

  logic pipe_busy;
  always_comb begin
    pipe_busy = a_v;
    for (int j = 0; j <= Q_W; j++) pipe_busy = pipe_busy | d_v[j];
  end

  // ---------------------------------------------------------------------------
  // Flush: final channel states, two bytes per output word
  // ---------------------------------------------------------------------------
  logic [CH_W-1:0] f_ch;
  logic            f_hi;
  logic [31:0]     f_x32;
  assign f_x32 = 32'(fresh[f_ch] ? L_INIT : st_mem[f_ch]);

  // ---------------------------------------------------------------------------
  // Output register
  // ---------------------------------------------------------------------------
  logic [15:0] o_data;
  logic [1:0]  o_keep;
  logic        o_last;
  assign m_tvalid = o_v;
  assign m_tdata  = o_data;
  assign m_tkeep  = o_keep;
  assign m_tlast  = o_last;

  // table writes (independent of output stalls)
  always_ff @(posedge clk) begin
    if (!rst_n) begin
      for (int k = 0; k < N_CTX; k++)
        for (int s = 0; s < N_SYM; s++)
          tbl[k * N_SYM + s] <= PB'(s * ((1 << PB) / N_SYM));
    end else if (tbl_we && tbl_ready) begin
      tbl[tbl_addr] <= tbl_data;
    end
  end

  always_ff @(posedge clk) begin
    if (!rst_n) begin
      phase    <= RUN;
      pkt_open <= 1'b0;
      fresh    <= '1;
      a_v      <= 1'b0;
      o_v      <= 1'b0;
      f_ch     <= '0;
      f_hi     <= 1'b0;
      for (int j = 0; j <= Q_W; j++) d_v[j] <= 1'b0;
    end else if (adv) begin
      // -- stage A ------------------------------------------------------------
      a_v <= fire;
      if (fire) begin
        a_x  <= in_x;
        a_f  <= in_f;
        a_c  <= in_c;
        a_ch <= s_tchan;
        fresh[s_tchan] <= 1'b0;
        pkt_open <= 1'b1;
      end

      // -- renorm -> D[0] and output ------------------------------------------
      d_v[0]   <= a_v;
      d_rem[0] <= F_W'(rn_x >> Q_W);
      d_low[0] <= rn_x[Q_W-1:0];
      d_q[0]   <= '0;
      d_f[0]   <= a_f;
      d_c[0]   <= a_c;
      d_ch[0]  <= a_ch;

      o_v    <= 1'b0;
      o_last <= 1'b0;
      if (a_v && rn_k != 2'd0) begin
        o_v    <= 1'b1;
        o_data <= a_x[15:0];
        o_keep <= (rn_k == 2'd2) ? 2'b11 : 2'b01;
      end

      // -- divider stages -----------------------------------------------------
      for (int j = 0; j < Q_W; j++) begin
        d_v[j+1]   <= d_v[j];
        d_rem[j+1] <= dv_ge[j] ? F_W'(dv_t[j] - {1'b0, d_f[j]}) : F_W'(dv_t[j]);
        d_low[j+1] <= d_low[j];
        d_q[j+1]   <= {d_q[j][Q_W-2:0], dv_ge[j]};
        d_f[j+1]   <= d_f[j];
        d_c[j+1]   <= d_c[j];
        d_ch[j+1]  <= d_ch[j];
      end

      // -- write-back ---------------------------------------------------------
      if (d_v[Q_W]) st_mem[d_ch[Q_W]] <= wb_x;

      // -- packet end ---------------------------------------------------------
      case (phase)
        RUN:   if (fire && s_tlast) phase <= DRAIN;
        DRAIN: if (!pipe_busy) begin
                 phase <= FLUSH;
                 f_ch  <= '0;
                 f_hi  <= 1'b0;
               end
        FLUSH: begin
                 o_v    <= 1'b1;
                 o_keep <= 2'b11;
                 o_data <= f_hi ? f_x32[31:16] : f_x32[15:0];
                 f_hi   <= !f_hi;
                 if (f_hi) begin
                   if (f_ch == CH_W'(N_CH - 1)) begin
                     o_last   <= 1'b1;
                     phase    <= RUN;
                     fresh    <= '1;
                     pkt_open <= 1'b0;
                   end
                   f_ch <= f_ch + 1'b1;
                 end
               end
        default: phase <= RUN;
      endcase
    end
  end

endmodule
