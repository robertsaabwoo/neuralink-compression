`default_nettype none
`timescale 1ns / 1ps

// nlc_rans: II=1 channel-interleaved static rANS for the lossy mode.
// Bit-exact against encode_core in model/nlc/rans_tdm.py (docstring = format)
// with the parameters of LossyConfig in model/nlc/lossy.py.
//
//   accept ─► A: ROM lookup, state read ─► renorm (0-2 bytes) + raw bytes out
//          ─► Q_W restoring-divide steps ─► WB: (q << PB) + r + c ─► state[ch]
//
// DIV_REG picks the registers between renorm, the divide steps and write-back.
// Default 0: all of it is one cycle after stage A (2 stages, ~121 cells deep at
// most, 110 ns slack at ss / 200 ns). All ones = the old one-bit-per-stage
// pipeline (+547 flops, same output bytes). The coder sees at most 8 symbols
// per 256 clocks, so the deep pipeline bought nothing.
//
// Differences from rans_tdm_static (src/robs_rANS), all for area:
//   - tables are a synthesised ROM (nlc_lossy_rom.v, scripts/gen_lossy_rom.py),
//     not 3 kbit of loadable registers;
//   - small state (LSH = 2: 22 bits, 3-byte flush, 10 divide steps);
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
    parameter int RAW_B = 2,       // raw bytes per escaped symbol
    // register after divider op k (bit k; op 0 = renorm, op k = quotient bit k):
    // all ones = one op per stage; 0 = renorm + divide + write-back in one cycle
    parameter logic [LSH+8:0] DIV_REG = '0
) (
    input  logic                   clk,
    input  logic                   rst_n,      // asynchronous clear of the control state
    output logic                   active,     // registers need a clock edge this cycle

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
      busy_ch   = busy_ch | (DIV_REG[j] && d_v[j] && d_ch[j] == s_tchan);
      pipe_busy = pipe_busy | (DIV_REG[j] && d_v[j]);
    end
  end

  assign s_tready = adv && phase == RUN && !busy_ch;
  logic fire;
  assign fire = s_tvalid && s_tready;

  // Parent clock gate: the coder's registers (and child gates) only see a clock
  // edge while it has work: a symbol accepted or in flight, a word to hand on,
  // or the packet end. Control registers clear asynchronously on rst_n.
  logic clk_c;
  assign active = adv && (fire || pipe_busy || o_v || phase != RUN);
  nlc_icg u_cg_c (.clk(clk), .en(active), .gclk(clk_c));

  // ---------------------------------------------------------------------------
  // Per-channel state, "not used yet this packet" flags, ROM
  // ---------------------------------------------------------------------------
  logic [N_CH-1:0][X_W-1:0] st_mem;           // clock-gated rows (written below)
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

  // stage A data: one clock gate, clocked only on an accepted symbol
  localparam int A_W = X_W + F_W + PB + CH_W + 1 + 8 * RAW_B;
  logic [A_W-1:0] a_d, a_q;
  assign a_d = {fresh[s_tchan] ? L_INIT : st_mem[s_tchan], in_fc[2*PB:PB], in_fc[PB-1:0],
                s_tchan, s_traw_v, s_traw};
  assign {a_x, a_f, a_c, a_ch, a_rv, a_raw} = a_q;
  nlc_greg #(.W(A_W)) u_a (.clk(clk_c), .en(rst_n && adv && fire), .d(a_d), .q(a_q));

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
  // Divider chain after stage A: op 0 = renorm, op k = quotient bit k (k = 1..Q_W),
  // then write-back. DIV_REG[k] puts a register after op k; a 0 leaves op k
  // combinational into op k+1. rem = partial remainder, dq = remaining dividend
  // bits (top first) with quotient bits shifted in at the bottom.
  // ---------------------------------------------------------------------------
  logic            c_v   [0:Q_W];               // op k's output
  logic [F_W-1:0]  c_rem [0:Q_W];
  logic [Q_W-1:0]  c_dq  [0:Q_W];
  logic [F_W-1:0]  c_f   [0:Q_W];
  logic [PB-1:0]   c_c   [0:Q_W];
  logic [CH_W-1:0] c_ch  [0:Q_W];
  logic [F_W-1:0]  d_rem [0:Q_W];               // its register (if DIV_REG[k])
  logic [Q_W-1:0]  d_dq  [0:Q_W];
  logic [F_W-1:0]  d_f   [0:Q_W];
  logic [PB-1:0]   d_c   [0:Q_W];

  logic            w_v;                         // running value along the chain
  logic [F_W-1:0]  w_rem, w_f;
  logic [Q_W-1:0]  w_dq;
  logic [PB-1:0]   w_c;
  logic [CH_W-1:0] w_ch;
  logic [F_W:0]    w_t;
  logic            w_ge;
  always_comb begin
    w_v   = a_v;
    w_rem = F_W'(rn_x >> Q_W);
    w_dq  = rn_x[Q_W-1:0];
    w_f   = a_f;
    w_c   = a_c;
    w_ch  = a_ch;
    c_v[0] = w_v; c_rem[0] = w_rem; c_dq[0] = w_dq; c_f[0] = w_f; c_c[0] = w_c; c_ch[0] = w_ch;
    for (int k = 1; k <= Q_W; k++) begin
      if (DIV_REG[k-1]) begin
        w_v = d_v[k-1]; w_rem = d_rem[k-1]; w_dq = d_dq[k-1];
        w_f = d_f[k-1]; w_c = d_c[k-1];     w_ch = d_ch[k-1];
      end
      w_t   = {w_rem, w_dq[Q_W-1]};
      w_ge  = w_t >= {1'b0, w_f};
      w_rem = w_ge ? F_W'(w_t - {1'b0, w_f}) : F_W'(w_t);
      w_dq  = {w_dq[Q_W-2:0], w_ge};
      c_v[k] = w_v; c_rem[k] = w_rem; c_dq[k] = w_dq; c_f[k] = w_f; c_c[k] = w_c; c_ch[k] = w_ch;
    end
    if (DIV_REG[Q_W]) begin
      w_v = d_v[Q_W]; w_rem = d_rem[Q_W]; w_dq = d_dq[Q_W];
      w_f = d_f[Q_W]; w_c = d_c[Q_W];     w_ch = d_ch[Q_W];
    end
  end

  // write-back: x' = (q << PB) + r + c   (r + c < 2^PB: an OR-free add)
  logic [X_W-1:0] wb_x;
  assign wb_x = {w_dq, PB'(w_rem + F_W'(w_c))};

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

  // output word: gated, written by a coded symbol with bytes or by a flush step
  logic             o_we;
  logic [8*O_B-1:0] o_data_d;
  logic [O_B-1:0]   o_keep_d;
  assign o_we     = rst_n && adv && (phase == FLUSH || (a_v && o_n != 3'd0));
  assign o_data_d = phase == FLUSH ? (8*O_B)'(f_sh[15:0])
                  : a_rv ? (8*O_B)'({a_x[15:0], a_raw}) : (8*O_B)'(a_x[15:0]);
  assign o_keep_d = phase == FLUSH ? ((32'(SB) - 32'(f_b) >= 2) ? O_B'(2'b11) : O_B'(2'b01))
                  : O_B'((1 << o_n) - 1);
  nlc_greg #(.W(8*O_B + O_B)) u_o (.clk(clk_c), .en(o_we), .d({o_data_d, o_keep_d}),
                                    .q({o_data, o_keep}));

  // channel state rows: gated, written back by the divider
  genvar gc;
  generate
    for (gc = 0; gc < N_CH; gc++) begin : g_st
      nlc_greg #(.W(X_W)) u_st (.clk(clk_c), .en(rst_n && adv && w_v && w_ch == CH_W'(gc)),
                                .d(wb_x), .q(st_mem[gc]));
    end
  endgenerate

  always_ff @(posedge clk_c or negedge rst_n) begin
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
      if (fire) fresh[s_tchan] <= 1'b0;   // stage A data: u_a

      // -- output (bytes leave from stage A) ------------------------------------
      o_v    <= 1'b0;
      o_last <= 1'b0;
      if (a_v && o_n != 3'd0) o_v <= 1'b1;   // word and keep: u_o

      // -- divider registers (unused ones are removed by synthesis) -----------
      for (int k = 0; k <= Q_W; k++) begin
        d_v[k] <= DIV_REG[k] && c_v[k];
        if (DIV_REG[k] && c_v[k]) begin
          d_rem[k] <= c_rem[k];
          d_dq[k]  <= c_dq[k];
          d_f[k]   <= c_f[k];
          d_c[k]   <= c_c[k];
          d_ch[k]  <= c_ch[k];
        end
      end

      // -- write-back: g_st ----------------------------------------------------

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
