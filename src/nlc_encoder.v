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
// Bytes out
//   Packets byte by byte into the output FIFO (wr_en, wr_data), wr_last with
//   the final byte of each packet. Held while fifo_full.
//
// Abort token (D6/D7): when the host has part of a packet (bytes of it written to the
// FIFO, not its last) and that packet is dropped (nlc_lossy abort: blocked output or
// short frame) or enable falls, an abort token (wr_abort) follows in the FIFO. The
// token logic is cleared by rst_n only, so it survives enable = 0.
//
// Packet format: model/nlc/packet.py and model/nlc/lossy.py (docstring):
//   header = mode[1:0] (= 1), seq[5:0]; payload; 256 frames per packet.
// ============================================================================
module nlc_encoder #(
    parameter ADC_BITS = 10,
    parameter N_SEL    = 8,
    parameter SEL_W    = $clog2(N_SEL),
    parameter FIFO_AW  = 3
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

    // output FIFO write port (nlc_out_fifo)
    output wire                 wr_en,
    output wire [7:0]           wr_data,
    output wire                 wr_last,
    output wire                 wr_abort,   // write the abort token (D6/D7)
    input  wire                 fifo_full,
    input  wire [FIFO_AW:0]     fifo_count
);

  wire       ly_valid, ly_last, ly_overflow, ly_abort;
  wire [7:0] ly_data;
  reg        tok, in_pkt, en_q;              // token owed, host holds part of a packet
  wire       tok_wr = tok && !fifo_full;

  nlc_lossy #(.N_SEL(N_SEL), .SEL_W(SEL_W)) u_lossy (
      .clk(clk), .rst_n(rst_n), .enable(enable), .n_sel(n_sel),
      .smp_valid(smp_valid), .smp_data(smp_data), .smp_ch(smp_ch), .smp_last(smp_last),
      .smp_tick(smp_tick), .smp_short(smp_short),
      .m_valid(ly_valid), .m_ready(!fifo_full && !tok), .m_data(ly_data), .m_last(ly_last),
      .overflow(ly_overflow), .abort_req(ly_abort)
  );

  wire data_wr = enable && ly_valid && !fifo_full && !tok;
  assign wr_en    = data_wr || tok_wr;
  assign wr_abort = tok_wr;
  assign wr_data  = tok_wr ? 8'h00 : ly_data;
  assign wr_last  = !tok_wr && ly_last;

  // the host holds part of a packet after this cycle's write
  wire in_pkt_nx = data_wr ? !ly_last : in_pkt;
  wire tok_set   = in_pkt_nx && (ly_abort || (en_q && !enable));
  wire gclk;
  nlc_icg u_cg (.clk(clk), .en(data_wr || tok_wr || tok_set || (enable != en_q)), .gclk(gclk));
  always @(posedge gclk or negedge rst_n) begin
    if (!rst_n) begin
      tok    <= 1'b0;
      in_pkt <= 1'b0;
      en_q   <= 1'b0;
    end else begin
      en_q <= enable;
      if (tok_wr) begin
        tok    <= 1'b0;
        in_pkt <= 1'b0;
      end else begin
        in_pkt <= in_pkt_nx;
        if (tok_set) tok <= 1'b1;
      end
    end
  end

  wire _unused = &{smp_first, fifo_count, ly_overflow, 1'b0};

endmodule
