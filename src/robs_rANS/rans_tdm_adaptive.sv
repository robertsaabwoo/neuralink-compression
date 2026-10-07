`default_nettype none
`timescale 1ns / 1ps

// rans_tdm_adaptive: II=1 pipelined, channel-interleaved, packet-adaptive rANS.
// Bit-exact against TdmRansEncoder in model/nlc/rans_tdm.py (docstring = format).
//
//   accept ─► A: table lookup, state read ─► renorm (0-2 bytes out)
//          ─► D[0..Q_W-1]: one quotient bit per stage ─► WB: q*M + r + c ─► state[ch]
//
// Hazard rule: a channel's state is read at accept and written back
// PIPE_DEPTH pipeline advances later, so the same channel must not be issued
// again within PIPE_DEPTH advances. Round-robin over N_CH >= PIPE_DEPTH
// channels satisfies this; valid gaps and output stalls only add distance.
//
// Packet end (s_tlast): input stalls, the pipeline drains, every channel's
// final state is emitted (4 bytes LE, channel 0..N_CH-1, m_tlast on the last
// word), then the next packet's table is installed.
module rans_tdm_adaptive #(
    parameter int N_SYM = 64,
    parameter int SYM_W = 6,       // $clog2(N_SYM)
    parameter int N_CH  = 32,      // >= PIPE_DEPTH
    parameter int CH_W  = 5,       // $clog2(N_CH)
    parameter int M_MAX = 4096,
    parameter int LSH   = 11       // L = M << LSH
) (
    input  logic             clk,
    input  logic             rst_n,

    input  logic             s_tvalid,
    output logic             s_tready,
    input  logic [SYM_W-1:0] s_tdata,
    input  logic [CH_W-1:0]  s_tchan,
    input  logic             s_tlast,    // last symbol of the packet

    output logic             m_tvalid,
    input  logic             m_tready,
    output logic [15:0]      m_tdata,    // first byte in [7:0]
    output logic [1:0]       m_tkeep,    // 2'b01: one byte, 2'b11: two bytes
    output logic             m_tlast
);

  localparam int M_W        = $clog2(M_MAX + 1);   // M, c_s, f_s
  localparam int Q_W        = LSH + 8;             // quotient bits: x < f_s << Q_W
  localparam int X_W        = Q_W + M_W;           // state: x < M << Q_W
  localparam int PIPE_DEPTH = Q_W + 3;             // accept -> state write-back
  localparam int COUNT_CAP  = M_MAX - N_SYM;

  // ---------------------------------------------------------------------------
  // Pipeline advance: everything moves unless the output register is stuck.
  // ---------------------------------------------------------------------------
  logic o_v;
  logic adv;
  assign adv = !(o_v && !m_tready);

  typedef enum logic [1:0] {RUN, DRAIN, FLUSH} phase_t;
  phase_t phase;

  assign s_tready = adv && (phase == RUN);
  logic fire;
  assign fire = s_tvalid && s_tready;

  // ---------------------------------------------------------------------------
  // Frequency model: active table (this packet) and next-packet histogram, both
  // stored as CDFs so c_s and f_s are direct reads (no prefix-sum tree).
  // ---------------------------------------------------------------------------
  logic [M_W-1:0] cdf_act [0:N_SYM];
  logic [M_W-1:0] cdf_acc [0:N_SYM];

  logic [M_W-1:0] m_cur;
  logic [M_W-1:0] in_c, in_f;
  logic [SYM_W:0] s_next;
  assign m_cur  = cdf_act[N_SYM];
  assign s_next = {1'b0, s_tdata} + 1'b1;
  assign in_c   = cdf_act[s_tdata];
  assign in_f   = cdf_act[s_next] - cdf_act[s_tdata];

  // ---------------------------------------------------------------------------
  // Per-channel state (context memory) and "not used yet this packet" flags.
  // ---------------------------------------------------------------------------
  logic [X_W-1:0]  st_mem [0:N_CH-1];
  logic [N_CH-1:0] fresh;
  logic [X_W-1:0]  l_cur;
  logic [X_W-1:0]  in_x;
  assign l_cur = X_W'(m_cur) << LSH;
  assign in_x  = fresh[s_tchan] ? l_cur : st_mem[s_tchan];

  // ---------------------------------------------------------------------------
  // Stage A registers
  // ---------------------------------------------------------------------------
  logic            a_v;
  logic [X_W-1:0]  a_x;
  logic [M_W-1:0]  a_f, a_c, a_m;
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
  logic [M_W-1:0]  d_rem [0:Q_W];
  logic [Q_W-1:0]  d_low [0:Q_W];
  logic [Q_W-1:0]  d_q   [0:Q_W];
  logic [M_W-1:0]  d_f   [0:Q_W];
  logic [M_W-1:0]  d_c   [0:Q_W];
  logic [M_W-1:0]  d_m   [0:Q_W];
  logic [CH_W-1:0] d_ch  [0:Q_W];

  logic [M_W:0]    dv_t  [0:Q_W-1];
  logic            dv_ge [0:Q_W-1];
  always_comb begin
    for (int j = 0; j < Q_W; j++) begin
      dv_t[j]  = {d_rem[j], d_low[j][Q_W-1-j]};
      dv_ge[j] = dv_t[j] >= {1'b0, d_f[j]};
    end
  end

  // write-back: x' = q * M + r + c
  logic [X_W-1:0] wb_x;
  assign wb_x = X_W'(d_q[Q_W]) * X_W'(d_m[Q_W]) + X_W'(d_rem[Q_W]) + X_W'(d_c[Q_W]);

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
  logic [X_W-1:0]  f_x;
  logic [31:0]     f_x32;
  assign f_x   = fresh[f_ch] ? l_cur : st_mem[f_ch];
  assign f_x32 = 32'(f_x);

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

  always_ff @(posedge clk) begin
    if (!rst_n) begin
      phase <= RUN;
      fresh <= '1;
      a_v   <= 1'b0;
      o_v   <= 1'b0;
      f_ch  <= '0;
      f_hi  <= 1'b0;
      for (int j = 0; j <= Q_W; j++) d_v[j] <= 1'b0;
      for (int t = 0; t <= N_SYM; t++) begin
        cdf_act[t] <= M_W'(t);
        cdf_acc[t] <= '0;
      end
    end else if (adv) begin
      // -- stage A ------------------------------------------------------------
      a_v <= fire;
      if (fire) begin
        a_x  <= in_x;
        a_f  <= in_f;
        a_c  <= in_c;
        a_m  <= m_cur;
        a_ch <= s_tchan;
        fresh[s_tchan] <= 1'b0;
        if (cdf_acc[N_SYM] < M_W'(COUNT_CAP)) begin
          for (int t = 1; t <= N_SYM; t++)
            if (t > s_tdata) cdf_acc[t] <= cdf_acc[t] + 1'b1;
        end
      end

      // -- renorm -> D[0] and output ------------------------------------------
      d_v[0]   <= a_v;
      d_rem[0] <= M_W'(rn_x >> Q_W);
      d_low[0] <= rn_x[Q_W-1:0];
      d_q[0]   <= '0;
      d_f[0]   <= a_f;
      d_c[0]   <= a_c;
      d_m[0]   <= a_m;
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
        d_rem[j+1] <= dv_ge[j] ? M_W'(dv_t[j] - {1'b0, d_f[j]}) : M_W'(dv_t[j]);
        d_low[j+1] <= d_low[j];
        d_q[j+1]   <= {d_q[j][Q_W-2:0], dv_ge[j]};
        d_f[j+1]   <= d_f[j];
        d_c[j+1]   <= d_c[j];
        d_m[j+1]   <= d_m[j];
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
                     o_last <= 1'b1;
                     phase  <= RUN;
                     fresh  <= '1;
                     for (int t = 0; t <= N_SYM; t++) begin
                       cdf_act[t] <= cdf_acc[t] + M_W'(t);
                       cdf_acc[t] <= '0;
                     end
                   end
                   f_ch <= f_ch + 1'b1;
                 end
               end
        default: phase <= RUN;
      endcase
    end
  end

endmodule
