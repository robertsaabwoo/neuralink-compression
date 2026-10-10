`default_nettype none

// ============================================================================
// nlc_encoder: the compression core. Lossy mode only (decision D1): the
// streaming 5/3 DWT + static rANS of src/nlc_lossy.sv. Modes 0/2/3 (lossless,
// binned spikes, spike-band power) exist in the Python model only (model/nlc).
//
// Contract (enforced bit-exactly by test/test_modes.py and test/core against model/nlc):
//
// Samples in
//   One selected-channel sample per smp_valid pulse, frame-major: channel 0,
//   1, ..., n_sel-1 of frame t, then frame t+1. smp_last flags channel
//   n_sel-1. The stream starts on a frame boundary after enable rises. There is
//   NO backpressure: every sample must be consumed. At most one sample per clock.
//
// Configuration
//   Stable while enable = 1. enable = 0 means idle: reset per-channel state
//   and the packet sequence number so the next run starts from packet 0.
//
// Bytes out (D8: valid-only stream, no back-pressure)
//   Packets byte by byte, one byte per clock while m_valid, m_last with the final
//   byte of each packet. The consumer takes every byte: nothing can hold the
//   output (scripts/proofs/output_bound.py: at the real frame rate the coder never
//   falls a frame behind, for any data).
//
// Abort token (D5/D7): when the consumer has part of a packet (bytes of it out, not
// its last) and that packet is dropped (nlc_lossy abort: short frame, or the coder a
// frame behind, which D8 rules out at the real frame rate) or enable falls, m_abort
// is high for one clock (with m_valid = 0) in the clock after. The lossy core
// outputs nothing in that clock (it is skipping or cleared), so the token needs no
// hold. The token logic is cleared by rst_n only, so it survives enable = 0.
//
// Packet format: model/nlc/packet.py and model/nlc/lossy.py (docstring):
//   header = mode[1:0] (= 1), seq[5:0]; payload; 256 frames per packet.
// ============================================================================
module nlc_encoder #(
    parameter ADC_BITS = 10,
    parameter N_SEL    = 8,
    parameter SEL_W    = $clog2(N_SEL)
) (
    input  wire                 clk,
    input  wire                 rst_n,

    // configuration (nlc_cfg)
    input  wire                 enable,
    input  wire [SEL_W:0]       n_sel,

    // selected-channel sample stream (nlc_slot_sel)
    input  wire                 smp_valid,
    input  wire [ADC_BITS-1:0]  smp_data,
    input  wire [SEL_W-1:0]     smp_ch,
    input  wire                 smp_first,
    input  wire                 smp_last,
    input  wire                 smp_tick,   // a new frame started (s_frame), not the first
    input  wire                 smp_short,  // with smp_tick: the frame that ended was short

    // packet byte stream (valid only, D8)
    output wire                 m_valid,
    output wire [7:0]           m_data,
    output wire                 m_last,     // with m_valid: the byte ends a packet
    output wire                 m_abort,    // abort token, one clock, m_valid = 0 (D5/D7)
    output wire                 overflow,   // sticky: the coder fell a frame behind
    output wire                 busy,       // needs clock edges while enable = 0
    input  wire [15:0]          dbg_hot,    // DFT (D11): one-hot gate group, 0 in normal mode
    output wire [7:0]           dbg_obs     // this block's gate enables in that group
);

  wire       ly_valid, ly_last, ly_abort;
  wire [7:0] ly_data, ly_obs;
  reg        tok, in_pkt, en_q;              // token owed, consumer holds part of a packet
  assign busy = en_q || tok;                 // the edge after enable falls, the token

  nlc_lossy #(.N_SEL(N_SEL), .SEL_W(SEL_W)) u_lossy (
      .clk(clk), .rst_n(rst_n), .enable(enable), .n_sel(n_sel),
      .smp_valid(smp_valid), .smp_data(smp_data), .smp_ch(smp_ch), .smp_last(smp_last),
      .smp_tick(smp_tick), .smp_short(smp_short),
      .m_valid(ly_valid), .m_data(ly_data), .m_last(ly_last),
      .overflow(overflow), .abort_req(ly_abort),
      .dbg_hot(dbg_hot), .dbg_obs(ly_obs)
  );

  // the byte stream straight from the serialiser (its state is registered); the
  // lossy core clears asynchronously on enable = 0, so enable only guards the edge
  assign m_valid = enable && ly_valid;
  assign m_data  = ly_data;
  assign m_last  = m_valid && ly_last;
  assign m_abort = tok;

  // the consumer holds part of a packet after this clock's byte
  wire in_pkt_nx = m_valid ? !ly_last : in_pkt;
  wire tok_set   = in_pkt_nx && (ly_abort || (en_q && !enable));
  wire gclk;
  wire cg_en = m_valid || tok || tok_set || (enable != en_q);
  nlc_icg u_cg (.clk(clk), .en(cg_en), .gclk(gclk));
  // gate group 13 bit 2 (scripts/dft/icg_map.py)
  assign dbg_obs = ly_obs | ({8{dbg_hot[13]}} & {5'd0, cg_en, 2'b00});
  always @(posedge gclk or negedge rst_n) begin
    if (!rst_n) begin
      tok    <= 1'b0;
      in_pkt <= 1'b0;
      en_q   <= 1'b0;
    end else begin
      en_q <= enable;
      if (tok) begin                         // the token is out this clock
        tok    <= 1'b0;
        in_pkt <= 1'b0;
      end else begin
        in_pkt <= in_pkt_nx;
        if (tok_set) tok <= 1'b1;
      end
    end
  end

`ifndef SYNTHESIS
  // the lossy core never has a byte in the token's clock (it skips or is cleared)
  always @(posedge clk) if (rst_n === 1'b1 && tok === 1'b1 && m_valid === 1'b1)
    $error("nlc_encoder: byte and abort token in the same clock");
`endif

  wire _unused = &{smp_first, 1'b0};

endmodule
