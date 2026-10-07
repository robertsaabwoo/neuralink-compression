`default_nettype none
`timescale 1ns / 1ps

// nlc_rans: II=1 channel-interleaved static rANS for the lossy mode.
// Bit-exact against encode_core in model/nlc/rans_tdm.py (docstring = format)
// with the parameters of LossyConfig in model/nlc/lossy.py.
//
//   accept ─► A: ROM lookup, state read ─► renorm (0-2 bytes) + raw bytes out
//          ─► D[0..Q_W-1]: one quotient bit per stage ─► WB: (q << PB) + r + c ─► state[ch]
//
// Differences from rans_tdm_static (src/robs_rANS), all for area:
//   - tables are a synthesised ROM (nlc_lossy_rom.v, scripts/gen_lossy_rom.py),
//     not 3 kbit of loadable registers;
//   - small state (LSH = 2: 22 bits, 3-byte flush, 10 divider stages);
//   - the divider shifts quotient bits into the dividend register (no q register);
//   - a symbol may carry RAW_B raw bytes, emitted before its renorm bytes (escapes);
//   - s_tready is low while the offered channel is still in the pipeline, so the
//     hazard rule is enforced here rather than left to the issuer.
module nlc_rans #(
    parameter int N_SYM = 64,
    parameter int SYM_W = 6,
    parameter int N_CH  = 8,
    parameter int CH_W  = 3,
    parameter int CTX_W = 2,
    parameter int PB    = 12,      // every table sums to 2^PB
    parameter int LSH   = 2,       // L = 2^PB << LSH
    parameter int SB    = 3,       // state bytes in the flush: ceil((PB + LSH + 8) / 8)
    parameter int RAW_B = 2        // raw bytes per escaped symbol
) (
    input  logic                   clk,
    input  logic                   rst_n,      // synchronous; also used as soft clear

    input  logic                   s_tvalid,
    output logic                   s_tready,
    input  logic [SYM_W-1:0]       s_tdata,
    input  logic [CTX_W-1:0]       s_tctx,
    input  logic [CH_W-1:0]        s_tchan,
    input  logic                   s_traw_v,   // send s_traw before this symbol's renorm bytes
    input  logic [8*RAW_B-1:0]     s_traw,     // little-endian
    input  logic                   s_tlast,    // last symbol of the packet

    output logic                   m_tvalid,
    input  logic                   m_tready,
    output logic [8*(RAW_B+2)-1:0] m_tdata,    // first byte in [7:0]
    output logic [RAW_B+1:0]       m_tkeep,    // contiguous from bit 0
    output logic                   m_tlast
);

  localparam int F_W  = PB + 1;               // f_s, remainder
  localparam int Q_W  = LSH + 8;              // quotient bits: x < f_s << Q_W
  localparam int X_W  = Q_W + PB;             // state: x < 2^PB << Q_W
  localparam int O_B  = RAW_B + 2;            // output bytes per word
  localparam logic [X_W-1:0] L_INIT = X_W'(1) << (PB + LSH);

  // ---------------------------------------------------------------------------
  // Pipeline advance: everything moves unless the output register is stuck.
  // ---------------------------------------------------------------------------
  logic o_v;
  logic adv;
  assign adv = !(o_v && !m_tready);

  typedef enum logic [1:0] {RUN, DRAIN, FLUSH} phase_t;
  phase_t phase;

  // channel in flight (stage A .. last divider stage, which writes back this cycle)
  logic            a_v;
  logic [CH_W-1:0] a_ch;
  logic            d_v  [0:Q_W];
  logic [CH_W-1:0] d_ch [0:Q_W];
  logic            busy_ch, pipe_busy;
  always_comb begin
    busy_ch   = a_v && a_ch == s_tchan;
    pipe_busy = a_v;
    for (int j = 0; j <= Q_W; j++) begin
      busy_ch   = busy_ch | (d_v[j] && d_ch[j] == s_tchan);
      pipe_busy = pipe_busy | d_v[j];
    end
  end

  assign s_tready = adv && phase == RUN && !busy_ch;
  logic fire;
  assign fire = s_tvalid && s_tready;

  // ---------------------------------------------------------------------------
  // Per-channel state, "not used yet this packet" flags, ROM
  // ---------------------------------------------------------------------------
  logic [X_W-1:0]  st_mem [0:N_CH-1];
  logic [N_CH-1:0] fresh;

  logic [2*PB:0] in_fc;                       // {f_s, c_s}
  nlc_lossy_rom u_rom (.addr({s_tctx, s_tdata}), .fc(in_fc));

  // ---------------------------------------------------------------------------
  // Stage A
  // ---------------------------------------------------------------------------
  logic [X_W-1:0]     a_x;
  logic [F_W-1:0]     a_f;
  logic [PB-1:0]      a_c;
  logic               a_rv;
  logic [8*RAW_B-1:0] a_raw;

  // renormalisation: 0, 1 or 2 bytes
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
  // Divider: d_rem = partial remainder, d_dq = remaining dividend bits (top
  // first) with quotient bits shifted in at the bottom.
  // ---------------------------------------------------------------------------
  logic [F_W-1:0] d_rem [0:Q_W];
  logic [Q_W-1:0] d_dq  [0:Q_W];
  logic [F_W-1:0] d_f   [0:Q_W];
  logic [PB-1:0]  d_c   [0:Q_W];

  logic [F_W:0] dv_t  [0:Q_W-1];
  logic         dv_ge [0:Q_W-1];
  always_comb begin
    for (int j = 0; j < Q_W; j++) begin
      dv_t[j]  = {d_rem[j], d_dq[j][Q_W-1]};
      dv_ge[j] = dv_t[j] >= {1'b0, d_f[j]};
    end
  end

  // write-back: x' = (q << PB) + r + c   (r + c < 2^PB: an OR-free add)
  logic [X_W-1:0] wb_x;
  assign wb_x = {d_dq[Q_W], PB'(d_rem[Q_W] + F_W'(d_c[Q_W]))};

  // ---------------------------------------------------------------------------
  // Flush: final channel states, SB bytes each, two bytes per output word
  // ---------------------------------------------------------------------------
  localparam int FB_W = $clog2(SB + 1);
  logic [CH_W-1:0] f_ch;
  logic [FB_W-1:0] f_b;
  logic [X_W-1:0]  f_x;
  logic [8*SB+7:0] f_sh;
  assign f_x  = fresh[f_ch] ? L_INIT : st_mem[f_ch];
  assign f_sh = (8*SB+8)'(f_x) >> (8 * f_b);

  // ---------------------------------------------------------------------------
  // Output register
  // ---------------------------------------------------------------------------
  logic [8*O_B-1:0] o_data;
  logic [O_B-1:0]   o_keep;
  logic             o_last;
  assign m_tvalid = o_v;
  assign m_tdata  = o_data;
  assign m_tkeep  = o_keep;
  assign m_tlast  = o_last;

  logic [2:0] o_n;      // bytes in this word (raw + renorm)
  assign o_n = (a_rv ? 3'(RAW_B) : 3'd0) + 3'(rn_k);

  always_ff @(posedge clk) begin
    if (!rst_n) begin
      phase <= RUN;
      fresh <= '1;
      a_v   <= 1'b0;
      o_v   <= 1'b0;
      f_ch  <= '0;
      f_b   <= '0;
      for (int j = 0; j <= Q_W; j++) d_v[j] <= 1'b0;
    end else if (adv) begin
      // -- stage A ------------------------------------------------------------
      a_v <= fire;
      if (fire) begin
        a_x   <= fresh[s_tchan] ? L_INIT : st_mem[s_tchan];
        a_f   <= in_fc[2*PB:PB];
        a_c   <= in_fc[PB-1:0];
        a_ch  <= s_tchan;
        a_rv  <= s_traw_v;
        a_raw <= s_traw;
        fresh[s_tchan] <= 1'b0;
      end

      // -- renorm -> D[0] and output ------------------------------------------
      d_v[0] <= a_v;
      if (a_v) begin
        d_rem[0] <= F_W'(rn_x >> Q_W);
        d_dq[0]  <= rn_x[Q_W-1:0];
        d_f[0]   <= a_f;
        d_c[0]   <= a_c;
        d_ch[0]  <= a_ch;
      end

      o_v    <= 1'b0;
      o_last <= 1'b0;
      if (a_v && o_n != 3'd0) begin
        o_v    <= 1'b1;
        o_data <= a_rv ? (8*O_B)'({a_x[15:0], a_raw}) : (8*O_B)'(a_x[15:0]);
        o_keep <= O_B'((1 << o_n) - 1);
      end

      // -- divider stages -----------------------------------------------------
      for (int j = 0; j < Q_W; j++) begin
        d_v[j+1] <= d_v[j];
        if (d_v[j]) begin
          d_rem[j+1] <= dv_ge[j] ? F_W'(dv_t[j] - {1'b0, d_f[j]}) : F_W'(dv_t[j]);
          d_dq[j+1]  <= {d_dq[j][Q_W-2:0], dv_ge[j]};
          d_f[j+1]   <= d_f[j];
          d_c[j+1]   <= d_c[j];
          d_ch[j+1]  <= d_ch[j];
        end
      end

      // -- write-back ---------------------------------------------------------
      if (d_v[Q_W]) st_mem[d_ch[Q_W]] <= wb_x;

      // -- packet end ---------------------------------------------------------
      case (phase)
        RUN:   if (fire && s_tlast) phase <= DRAIN;
        DRAIN: if (!pipe_busy) begin
                 phase <= FLUSH;
                 f_ch  <= '0;
                 f_b   <= '0;
               end
        FLUSH: begin
                 o_v    <= 1'b1;
                 o_data <= (8*O_B)'(f_sh[15:0]);
                 o_keep <= (32'(SB) - 32'(f_b) >= 2) ? O_B'(2'b11) : O_B'(2'b01);
                 if (32'(f_b) + 2 >= SB) begin
                   f_b <= '0;
                   if (f_ch == CH_W'(N_CH - 1)) begin
                     o_last <= 1'b1;
                     phase  <= RUN;
                     fresh  <= '1;
                   end
                   f_ch <= f_ch + 1'b1;
                 end else begin
                   f_b <= f_b + 2'd2;
                 end
               end
        default: phase <= RUN;
      endcase
    end
  end

endmodule
