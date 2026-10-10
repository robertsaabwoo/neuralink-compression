`default_nettype none
`timescale 1ns / 1ps

// nlc_rans: channel-interleaved static rANS for the lossy mode, DIV_K quotient bits per clock.
// Bit-exact against encode_core in model/nlc/rans_tdm.py (docstring = format)
// with the parameters of LossyConfig in model/nlc/lossy.py.
//
//   offered symbol ─► state read + ROM ─► renorm (0-2 bytes) + raw bytes ─► output word
//                                     └─► DK divide steps ─► x_l ─► DK steps ... ─► WB: state[ch]
//
// No input register: the issuer holds its symbol (valid/ready) until the coder takes it,
// so the ROM, the raw bytes and the channel are read straight from the issuer for all
// NC = ceil(Q_W / DIV_K) cycles of the divide. The first cycle renormalises, sends the
// word and runs DK steps; the symbol is taken (s_tready) in the last cycle, which writes
// the new state back. The next symbol reads the state a cycle later: no hazard logic.
// DIV_K >= Q_W: everything in one cycle, II = 1 (no loop register).
//
// Differences from rans_tdm_static (test/rans/ref), all for area:
//   - tables are a synthesised ROM (nlc_lossy_rom.v, scripts/gen_lossy_rom.py),
//     not 3 kbit of loadable registers;
//   - small state (LSH = 2: 22 bits, 3-byte flush, 10 divide steps);
//   - the divider shifts quotient bits into the dividend register (no q register);
//   - one state read port, shared by the coder and the flush;
//   - a symbol may carry RAW_B raw bytes, emitted before its renorm bytes (escapes).
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
    parameter int DIV_K = 5,       // quotient bits per clock (>= LSH + 8: one cycle)
    parameter bit LATCH_ROWS = 1'b1 // channel states in latch rows (nlc_lreg), else flops
) (
    input  logic                   clk,
    input  logic                   rst_n,      // asynchronous clear of the control state
    output logic                   active,     // registers need a clock edge this cycle

    input  logic                   s_tvalid,   // held, with the data, until s_tready
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

  localparam int F_W  = PB + 1;               // f_s
  localparam int Q_W  = LSH + 8;              // quotient bits: x < f_s << Q_W
  localparam int X_W  = Q_W + PB;             // state: x < 2^PB << Q_W
  localparam int O_B  = RAW_B + 2;            // output bytes per word
  localparam logic [X_W-1:0] L_INIT = X_W'(1) << (PB + LSH);
  localparam int DK     = DIV_K < Q_W ? DIV_K : Q_W;   // divide steps per cycle
  localparam int NC     = (Q_W + DK - 1) / DK;         // cycles per symbol
  localparam int LAST_K = Q_W - (NC - 1) * DK;         // steps that count in the last one
  localparam bit LOOP   = NC > 1;
  localparam int IC_W   = NC > 2 ? $clog2(NC - 1) : 1;

  // ---------------------------------------------------------------------------
  // Handshake. adv: the output word register can be written (or is empty).
  // a_v: an offered symbol in its first cycle (renorm + word + first steps);
  // it_v: a symbol in divide cycles 2..NC, it_c = cycles left after this one.
  // ---------------------------------------------------------------------------
  logic o_v, adv;
  assign adv = !(o_v && !m_tready);

  logic phase;                                // 0: run, 1: flush the states
  logic            it_v;
  logic [IC_W-1:0] it_c;
  logic            a_v, start, fire;
  assign a_v   = s_tvalid && !phase && !it_v;
  assign start = a_v && adv;
  assign fire  = LOOP ? it_v && it_c == '0 : start;
  assign s_tready = LOOP ? it_v && it_c == '0 : adv && !phase;

  // Parent clock gate: the coder's registers (and child gates) only see a clock
  // edge while it has work. Control registers clear asynchronously on rst_n.
  logic clk_c;
  assign active = adv && (a_v || o_v || phase) || it_v;
  nlc_icg u_cg_c (.clk(clk), .en(active), .gclk(clk_c));

  // ---------------------------------------------------------------------------
  // Per-channel state, "not used yet this packet" flags, ROM, one read port
  // ---------------------------------------------------------------------------
  logic [N_CH-1:0][X_W-1:0] st_mem;           // clock-gated rows (written below)
  logic [N_CH-1:0] fresh;
  logic [CH_W-1:0] f_ch;                      // flush: channel
  logic [CH_W-1:0] rd_ch;
  logic [X_W-1:0]  x_rd;
  assign rd_ch = phase ? f_ch : s_tchan;
  assign x_rd  = fresh[rd_ch] ? L_INIT : st_mem[rd_ch];

  logic [2*PB:0]  in_fc;                      // {f_s, c_s}
  logic [F_W-1:0] f_s;
  logic [PB-1:0]  c_s;
  nlc_lossy_rom u_rom (.addr({s_tctx, s_tdata}), .fc(in_fc));
  assign f_s = in_fc[2*PB:PB];
  assign c_s = in_fc[PB-1:0];

  // renormalisation: 0, 1 or 2 bytes
  logic [X_W:0]   thr;
  logic [1:0]     rn_k;
  logic [X_W-1:0] rn_x;
  assign thr = (X_W + 1)'(f_s) << Q_W;
  always_comb begin
    if ({1'b0, x_rd} >= thr) begin
      if ({9'd0, x_rd[X_W-1:8]} >= thr) begin
        rn_k = 2'd2;
        rn_x = x_rd >> 16;
      end else begin
        rn_k = 2'd1;
        rn_x = x_rd >> 8;
      end
    end else begin
      rn_k = 2'd0;
      rn_x = x_rd;
    end
  end

  // ---------------------------------------------------------------------------
  // Divider: DK restoring steps a cycle. rem = partial remainder (< f_s <= 2^PB),
  // dq = remaining dividend bits (top first) with quotient bits shifted in at the
  // bottom, so {rem, dq} fits the X_W-bit loop register x_l.
  // ---------------------------------------------------------------------------
  logic [X_W-1:0] x_l;
  logic [F_W-1:0] l_rem [0:DK];
  logic [Q_W-1:0] l_dq  [0:DK];
  logic [F_W:0]   l_t;
  logic           l_ge;
  always_comb begin
    if (LOOP && it_v) begin
      l_rem[0] = F_W'(x_l[X_W-1:Q_W]);
      l_dq[0]  = x_l[Q_W-1:0];
    end else begin
      l_rem[0] = F_W'(rn_x >> Q_W);
      l_dq[0]  = rn_x[Q_W-1:0];
    end
    for (int k = 1; k <= DK; k++) begin
      l_t      = {l_rem[k-1], l_dq[k-1][Q_W-1]};
      l_ge     = l_t >= {1'b0, f_s};
      l_rem[k] = l_ge ? F_W'(l_t - {1'b0, f_s}) : F_W'(l_t);
      l_dq[k]  = {l_dq[k-1][Q_W-2:0], l_ge};
    end
  end

  // write-back: x' = (q << PB) + r + c   (r + c < 2^PB: an OR-free add)
  logic [X_W-1:0] wb_x;
  assign wb_x = {l_dq[LAST_K], PB'(l_rem[LAST_K] + F_W'(c_s))};

  // Latch rows (LATCH_ROWS): a state row is open for the high phase after its write edge
  // (fire), while the issuer's next symbol (c_s) already changes wb_x, so the row is
  // written from a register loaded at that edge. With the loop, x_l is free then (no
  // symbol starts in a fire cycle: a_v needs !it_v) and takes wb_x at fire; without it,
  // a separate staging register does.
  logic [X_W-1:0] wb_sg;                      // the state row's data
  generate
    if (LOOP) begin : g_loop
      logic xl_wb;                            // x_l stages the write-back
      assign xl_wb = LATCH_ROWS && fire;
      nlc_greg #(.W(X_W)) u_xl (.clk(clk), .en(rst_n && (start || (it_v && it_c != '0) || xl_wb)),
                                .d(xl_wb ? wb_x : {l_rem[DK][PB-1:0], l_dq[DK]}), .q(x_l));
      assign wb_sg = LATCH_ROWS ? x_l : wb_x;
    end else begin : g_noloop
      assign x_l = '0;
      if (LATCH_ROWS) begin : g_sg
        nlc_greg #(.W(X_W)) u_wb (.clk(clk), .en(rst_n && fire), .d(wb_x), .q(wb_sg));
      end else begin : g_nosg
        assign wb_sg = wb_x;
      end
    end
  endgenerate

  // ---------------------------------------------------------------------------
  // Output register: a coded symbol's raw + renorm bytes, or two flush bytes
  // ---------------------------------------------------------------------------
  localparam int FB_W = $clog2(SB + 1);
  logic [FB_W-1:0] f_b;                       // flush: byte of the state
  logic [8*SB+7:0] f_sh;
  assign f_sh = (8*SB+8)'(x_rd) >> (8 * f_b);

  logic [8*O_B-1:0] o_data;
  logic [O_B-1:0]   o_keep;
  logic             o_last;
  assign m_tvalid = o_v;
  assign m_tdata  = o_data;
  assign m_tkeep  = o_keep;
  assign m_tlast  = o_last;

  logic [2:0] o_n;      // bytes in this word (raw + renorm)
  assign o_n = (s_traw_v ? 3'(RAW_B) : 3'd0) + 3'(rn_k);

  logic             o_we;
  logic [8*O_B-1:0] o_data_d;
  logic [O_B-1:0]   o_keep_d;
  assign o_we     = rst_n && adv && (phase || (a_v && o_n != 3'd0));
  assign o_data_d = phase ? (8*O_B)'(f_sh[15:0])
                  : s_traw_v ? (8*O_B)'({x_rd[15:0], s_traw}) : (8*O_B)'(x_rd[15:0]);
  assign o_keep_d = phase ? ((32'(SB) - 32'(f_b) >= 2) ? O_B'(2'b11) : O_B'(2'b01))
                  : O_B'((1 << o_n) - 1);
  // The data gates (u_xl, u_o, g_st) hang off clk, not clk_c: their enables imply `active`
  // (clk_c's enable), so it is the same clock, one gate level shallower, the same depth
  // as the issuer (clk_i) that feeds them: CTS without latency balancing then needs
  // no hold buffers on these paths (area experiment G, docs/results.md).
  nlc_greg #(.W(8*O_B + O_B)) u_o (.clk(clk), .en(o_we), .d({o_data_d, o_keep_d}),
                                    .q({o_data, o_keep}));

  // channel state rows: gated, written by the symbol's last divide cycle (from wb_sg)
  genvar gc;
  generate
    for (gc = 0; gc < N_CH; gc++) begin : g_st
      nlc_rreg #(.W(X_W), .LATCH(LATCH_ROWS)) u_st (
          .clk(clk), .en(rst_n && fire && s_tchan == CH_W'(gc)), .d(wb_sg), .q(st_mem[gc]));
    end
  endgenerate

  // ---------------------------------------------------------------------------
  // Control
  // ---------------------------------------------------------------------------
  always_ff @(posedge clk_c or negedge rst_n) begin
    if (!rst_n) begin
      it_v <= 1'b0;
      it_c <= '0;
    end else if (LOOP && start) begin
      it_v <= 1'b1;
      it_c <= IC_W'(NC - 2);
    end else if (it_v) begin
      it_c <= it_c - 1'b1;
      if (it_c == '0) it_v <= 1'b0;
    end
  end

  always_ff @(posedge clk_c or negedge rst_n) begin
    if (!rst_n) begin
      phase <= 1'b0;
      fresh       <= '1;
      o_v         <= 1'b0;
      o_last      <= 1'b0;
      f_ch        <= '0;
      f_b         <= '0;
    end else begin
      if (fire) fresh[s_tchan] <= 1'b0;
      if (fire && s_tlast) phase <= 1'b1;       // the last word is out first (adv)
      if (adv) begin
        o_v    <= a_v && o_n != 3'd0 || phase;
        o_last <= 1'b0;
        if (phase) begin
          if (32'(f_b) + 2 >= SB) begin
            f_b <= '0;
            if (f_ch == CH_W'(N_CH - 1)) begin
              o_last      <= 1'b1;
              phase <= 1'b0;
              fresh       <= '1;
              f_ch        <= '0;
            end else begin
              f_ch <= f_ch + 1'b1;
            end
          end else begin
            f_b <= f_b + 2'd2;
          end
        end
      end
    end
  end

endmodule
