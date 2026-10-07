`default_nettype none

// ============================================================================
// nlc_encoder: THE COMPRESSION CORE. Write your design here.
//
// Contract (enforced bit-exactly by test/test_modes.py against model/nlc):
//
// Samples in
//   One selected-channel sample per smp_valid pulse, frame-major: channel 0,
//   1, ..., n_sel-1 of frame t, then frame t+1. smp_first / smp_last flag
//   channel 0 / channel n_sel-1. The stream starts on a frame boundary after
//   enable rises. There is NO backpressure: every sample must be consumed.
//   smp_valid pulses are at least SLOT_CYCLES clocks apart (10 at 50 MHz for
//   a 256-slot, 19.3 kHz ADC mux; the testbench default).
//
// Configuration
//   Stable while enable = 1. enable = 0 means idle: reset per-channel state
//   and the packet sequence number so the next run starts from packet 0.
//
// Bytes out
//   Write packets byte by byte into the output FIFO (wr_en, wr_data). Assert
//   wr_last with the final byte of each packet. Check fifo_full before
//   writing: a write while full is dropped and sets the sticky overflow flag.
//
// Packet format (model/nlc/packet.py and each mode's module docstring):
//   header = mode[1:0], seq[5:0]; mode payload; zero pad to a byte boundary.
//   lossless (model/nlc/lossless.py): fpp frames per packet.
//   binned   (model/nlc/spikes.py):   one packet per win_len frames.
//   sbp      (model/nlc/spikes.py):   one packet per win_len frames.
//   lossy    (model/nlc/lossy.py):    256 frames per packet; done (nlc_lossy.sv).
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
    input  wire [1:0]           mode,
    input  wire [SEL_W:0]       n_sel,
    input  wire [7:0]           fpp,
    input  wire [15:0]          win_len,
    input  wire [4:0]           sbp_shift,
    input  wire [8*N_SEL-1:0]   thr,        // channel i: thr[8*i +: 8]

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
`include "nlc_regs.vh"

  // Fixed in hardware. Must equal the HW_* constants in test/regs.py, which
  // check that the golden-model vectors were generated with the same values.
  localparam RICE_K_MAX   = 9;
  localparam RICE_Q_LIMIT = 16;
  localparam RICE_N_RESET = 64;
  localparam RICE_A_INIT  = 16;
  localparam FE_HP_SHIFT  = 3;
  localparam FE_REFRACT   = 20;
  localparam BIN_GROUP    = 4;
  localparam BIN_C_MAX    = 7;
  localparam SBP_BITS     = 8;

  // --------------------------------------------------------------------------
  // YOUR CODE HERE
  //   suggested blocks: per-channel state (x_prev, A, N, first flag),
  //   Rice datapath, packet framer (header, frame counter, seq), bit packer.
  // --------------------------------------------------------------------------

  // --------------------------------------------------------------------------
  // Mode 1: lossy (src/nlc_lossy.sv: streaming 5/3 DWT + static rANS).
  // Its byte stream already carries the packet header. Other modes: give
  // them their own wr_* sources and select on `mode` below.
  // --------------------------------------------------------------------------
  wire       lossy_en = enable && (mode == MODE_LOSSY);
  wire       ly_valid, ly_last, ly_overflow;
  wire [7:0] ly_data;

  nlc_lossy #(.N_SEL(N_SEL), .SEL_W(SEL_W)) u_lossy (
      .clk(clk), .rst_n(rst_n), .enable(lossy_en), .n_sel(n_sel),
      .smp_valid(smp_valid), .smp_data(smp_data), .smp_ch(smp_ch), .smp_last(smp_last),
      .m_valid(ly_valid), .m_ready(!fifo_full), .m_data(ly_data), .m_last(ly_last),
      .overflow(ly_overflow)
  );

  assign wr_en   = lossy_en && ly_valid && !fifo_full;
  assign wr_data = ly_data;
  assign wr_last = ly_last;

endmodule
