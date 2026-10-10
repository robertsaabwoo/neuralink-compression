`default_nettype none

// Compression core: the block that would sit between the ADC mux and the
// merge circuitry on an N1-class chip.
//
//   ADC stream ──► nlc_slot_sel ──► nlc_encoder ──► merge / host  (valid-only byte stream,
//                                       ▲               no back-pressure: decision D8)
//   cfg port ───► nlc_cfg ──────────────┘
//
// DFT debug modes (D11, REG_DBG, written while enable = 0):
//   0 normal   compressed packets (bit-exact; the debug logic below sees constant inputs)
//   1 raw      the encoder is held disabled (cleared, no clock edge) and the output carries
//              the slot selector's samples: per sample {6'b0, s[9:8]} then s[7:0], channels
//              in slot order, m_last on the low byte of channel n_sel-1. Needs one free clock
//              after every selected sample (the TT pin path always has it; at one slot per
//              clock no two selected slots may be adjacent): a sample arriving while the
//              previous low byte is due replaces it and sets the sticky overflow.
//   2 icg      the gate enables of group DBG[7:4] on dbg_obs (the TT top registers them on
//              uo_out); the core runs normally
module nlc_core #(
    parameter ADC_BITS   = 10,
    parameter N_SEL      = 8
) (
    input  wire                 clk,
    input  wire                 rst_n,
    // ADC mux stream: one sample per s_valid, slot 0 flagged by s_frame, no backpressure
    input  wire                 s_valid,
    input  wire                 s_frame,
    input  wire [ADC_BITS-1:0]  s_data,
    output wire                 s_want,     // the next s_valid is a selected slot
    // configuration write port
    input  wire                 cfg_we,
    input  wire [7:0]           cfg_addr,
    input  wire [7:0]           cfg_data,
    // packet byte stream: one byte per clock while m_valid, the consumer takes every byte
    // (no back-pressure, D8)
    output wire [7:0]           m_data,
    output wire                 m_valid,
    output wire                 m_last,     // with m_valid: the byte ends a packet
    output wire                 m_abort,    // abort token (D5/D7), one clock, m_valid = 0:
                                            // drop the partial packet
    output wire                 enabled,    // CTRL.enable
    output wire [7:0]           cfg_rdata,  // readback of the register at cfg_addr (DFT)
    output wire                 overflow,   // sticky: the coder fell a frame behind
                                            // (raw mode: a low byte was lost)
    // DFT (D11): static between config writes
    output wire                 dbg_icg,    // mode 2: the TT top shows dbg_obs on uo_out
    output wire                 dbg_top,    // mode 2, group 15: the TT top adds its gates
    output wire [7:0]           dbg_obs     // gate enables of the selected group (core part)
);

  localparam SEL_W = $clog2(N_SEL);

  wire                enable;
  wire                enc_busy;

  // One gate in front of the whole core: disabled (enable = 0) with the output drained,
  // no child gate sees a clock edge either (idle power, docs/results.md F20). Control
  // state clears asynchronously on disable; edges are needed only for a config write
  // and the encoder's disable edge / abort token (busy).
  wire clk_core;
  wire en_core = !rst_n || enable || cfg_we || enc_busy;
  nlc_icg u_cg_core (.clk(clk), .en(en_core), .gclk(clk_core));
  wire [SEL_W:0]      n_sel;
  wire [8*N_SEL-1:0]  sel_slots;

  // The config registers' own gate (enable cfg_we) hangs off clk, not clk_core (cfg_we is
  // one of clk_core's enables, so this is the same clock): the same gate depth as the TT pin
  // registers that feed them, so CTS without latency balancing needs no hold buffers on
  // the 64-bit slot table (area experiment G, docs/results.md).
  nlc_cfg #(.N_SEL(N_SEL)) u_cfg (
      .clk(clk), .rst_n(rst_n),
      .cfg_we(cfg_we), .cfg_addr(cfg_addr), .cfg_data(cfg_data),
      .enable(enable), .n_sel(n_sel), .sel_slots(sel_slots), .rdata(cfg_rdata),
      .dbg_raw(dbg_raw), .dbg_icg(dbg_icg), .dbg_hot(dbg_hot), .dbg_obs(cfg_obs)
  );
  assign enabled = enable;
  assign dbg_top = dbg_hot[15];

  // raw bypass (mode 1): the encoder sees enable = 0. Glitch-free: DBG is written only while
  // enable = 0, so enable and dbg_raw never change at the same edge.
  wire enc_enable = enable && !dbg_raw;

  wire                smp_valid;
  wire [ADC_BITS-1:0] smp_data;
  wire [SEL_W-1:0]    smp_ch;
  wire                smp_first;
  wire                smp_last;
  wire                smp_tick;
  wire                smp_short;
  wire                dbg_raw;
  wire [15:0]         dbg_hot;
  wire [7:0]          cfg_obs, sel_obs, enc_obs;

  nlc_slot_sel #(.ADC_BITS(ADC_BITS), .N_SEL(N_SEL)) u_sel (
      .clk(clk_core), .rst_n(rst_n), .enable(enable), .n_sel(n_sel), .sel_slots(sel_slots),
      .s_valid(s_valid), .s_frame(s_frame), .s_data(s_data), .s_want(s_want),
      .smp_valid(smp_valid), .smp_data(smp_data), .smp_ch(smp_ch),
      .smp_first(smp_first), .smp_last(smp_last), .smp_tick(smp_tick), .smp_short(smp_short),
      .dbg_hot(dbg_hot), .dbg_obs(sel_obs)
  );

  nlc_encoder #(.ADC_BITS(ADC_BITS), .N_SEL(N_SEL)) u_enc (
      .clk(clk_core), .rst_n(rst_n),
      .enable(enc_enable), .n_sel(n_sel),
      .smp_valid(smp_valid), .smp_data(smp_data), .smp_ch(smp_ch),
      .smp_first(smp_first), .smp_last(smp_last), .smp_tick(smp_tick), .smp_short(smp_short),
      .m_valid(enc_valid), .m_data(enc_data), .m_last(enc_last), .m_abort(m_abort),
      .overflow(enc_ovf), .busy(enc_busy), .dbg_hot(dbg_hot), .dbg_obs(enc_obs)
  );

  wire       enc_valid, enc_last, enc_ovf;
  wire [7:0] enc_data;

  // Raw bypass (mode 1). raw_lo: the low byte of smp_data is due this clock (the slot
  // selector holds the sample until its next hit, at least 2 clocks on the TT pin path).
  // Own gate and async clear on rst_n && enable && dbg_raw (flops, one changes per edge):
  // in normal mode no edge and no toggle.
  reg  raw_lo, raw_ovf;
  wire raw_clr_n = rst_n && enable && dbg_raw;
  wire en_raw    = raw_clr_n && (smp_valid || raw_lo);
  wire clk_raw;
  nlc_icg u_cg_raw (.clk(clk_core), .en(en_raw), .gclk(clk_raw));
  always @(posedge clk_raw or negedge raw_clr_n) begin
    if (!raw_clr_n) begin
      raw_lo  <= 1'b0;
      raw_ovf <= 1'b0;
    end else begin
      raw_lo  <= smp_valid;
      raw_ovf <= raw_ovf || (smp_valid && raw_lo);     // the due low byte was replaced
    end
  end

  wire raw_valid = dbg_raw && (smp_valid || raw_lo);
  assign m_valid  = dbg_raw ? raw_valid : enc_valid;
  assign m_data   = !dbg_raw ? enc_data :
                    smp_valid ? {6'd0, smp_data[9:8]} : smp_data[7:0];
  assign m_last   = dbg_raw ? (raw_lo && !smp_valid && smp_last) : enc_last;
  assign overflow = enc_ovf || raw_ovf;

  // gate group 13 bits 6 / 7 (scripts/dft/icg_map.py); dbg_hot = 0 in normal mode
  assign dbg_obs = cfg_obs | sel_obs | enc_obs | ({8{dbg_hot[13]}} & {en_raw, en_core, 6'd0});

endmodule
