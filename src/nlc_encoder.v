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

    // output FIFO write port (nlc_out_fifo)
    output wire                 wr_en,
    output wire [7:0]           wr_data,
    output wire                 wr_last,
    input  wire                 fifo_full,
    input  wire [FIFO_AW:0]     fifo_count
);

  wire       ly_valid, ly_last, ly_overflow;
  wire [7:0] ly_data;

  nlc_lossy #(.N_SEL(N_SEL), .SEL_W(SEL_W)) u_lossy (
      .clk(clk), .rst_n(rst_n), .enable(enable), .n_sel(n_sel),
      .smp_valid(smp_valid), .smp_data(smp_data), .smp_ch(smp_ch), .smp_last(smp_last),
      .m_valid(ly_valid), .m_ready(!fifo_full), .m_data(ly_data), .m_last(ly_last),
      .overflow(ly_overflow)
  );

  assign wr_en   = enable && ly_valid && !fifo_full;
  assign wr_data = ly_data;
  assign wr_last = ly_last;

  wire _unused = &{smp_first, fifo_count, ly_overflow, 1'b0};

endmodule
