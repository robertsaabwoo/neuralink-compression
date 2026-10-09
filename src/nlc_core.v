`default_nettype none

// Compression core: the block that would sit between the ADC mux and the
// merge circuitry on an N1-class chip.
//
//   ADC stream ──► nlc_slot_sel ──► nlc_encoder ──► nlc_out_fifo ──► merge / host
//                                       ▲
//   cfg port ───► nlc_cfg ──────────────┘
module nlc_core #(
    parameter ADC_BITS   = 10,
    parameter N_SEL      = 8,
    parameter FIFO_DEPTH = 8
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
    // packet byte stream
    output wire [7:0]           m_data,
    output wire                 m_valid,
    output wire                 m_last,
    output wire                 m_abort,    // abort token (D6/D7): drop the partial packet
    input  wire                 m_ready,
    output wire                 overflow
);

  localparam SEL_W   = $clog2(N_SEL);
  localparam FIFO_AW = $clog2(FIFO_DEPTH);

  wire                enable;
  wire                enc_busy;

  // One gate in front of the whole core: disabled (enable = 0) with the output drained,
  // no child gate sees a clock edge either (idle power, docs/results.md F20). Control
  // state clears asynchronously on disable; edges are needed only for a config write,
  // the FIFO draining (m_valid) and the encoder's disable edge / abort token (busy).
  wire clk_core;
  nlc_icg u_cg_core (.clk(clk), .en(!rst_n || enable || cfg_we || m_valid || enc_busy),
                     .gclk(clk_core));
  wire [SEL_W:0]      n_sel;
  wire [8*N_SEL-1:0]  sel_slots;

  // The config registers' own gate (enable cfg_we) hangs off clk, not clk_core (cfg_we is
  // one of clk_core's enables, so this is the same clock): the same gate depth as the TT pin
  // registers that feed them, so CTS without latency balancing needs no hold buffers on
  // the 64-bit slot table (area experiment G, docs/results.md).
  nlc_cfg #(.N_SEL(N_SEL)) u_cfg (
      .clk(clk), .rst_n(rst_n),
      .cfg_we(cfg_we), .cfg_addr(cfg_addr), .cfg_data(cfg_data),
      .enable(enable), .n_sel(n_sel), .sel_slots(sel_slots)
  );

  wire                smp_valid;
  wire [ADC_BITS-1:0] smp_data;
  wire [SEL_W-1:0]    smp_ch;
  wire                smp_first;
  wire                smp_last;
  wire                smp_tick;
  wire                smp_short;

  nlc_slot_sel #(.ADC_BITS(ADC_BITS), .N_SEL(N_SEL)) u_sel (
      .clk(clk_core), .rst_n(rst_n), .enable(enable), .n_sel(n_sel), .sel_slots(sel_slots),
      .s_valid(s_valid), .s_frame(s_frame), .s_data(s_data), .s_want(s_want),
      .smp_valid(smp_valid), .smp_data(smp_data), .smp_ch(smp_ch),
      .smp_first(smp_first), .smp_last(smp_last), .smp_tick(smp_tick), .smp_short(smp_short)
  );

  wire                wr_en;
  wire [7:0]          wr_data;
  wire                wr_last;
  wire                wr_abort;
  wire                fifo_full;
  wire [FIFO_AW:0]    fifo_count;

  nlc_encoder #(.ADC_BITS(ADC_BITS), .N_SEL(N_SEL), .FIFO_AW(FIFO_AW)) u_enc (
      .clk(clk_core), .rst_n(rst_n),
      .enable(enable), .n_sel(n_sel),
      .smp_valid(smp_valid), .smp_data(smp_data), .smp_ch(smp_ch),
      .smp_first(smp_first), .smp_last(smp_last), .smp_tick(smp_tick), .smp_short(smp_short),
      .wr_en(wr_en), .wr_data(wr_data), .wr_last(wr_last), .wr_abort(wr_abort),
      .fifo_full(fifo_full), .fifo_count(fifo_count), .busy(enc_busy)
  );

  nlc_out_fifo #(.DEPTH(FIFO_DEPTH)) u_fifo (
      .clk(clk_core), .rst_n(rst_n),
      .wr_en(wr_en), .wr_data(wr_data), .wr_last(wr_last), .wr_abort(wr_abort),
      .full(fifo_full), .count(fifo_count),
      .m_valid(m_valid), .m_data(m_data), .m_last(m_last), .m_abort(m_abort), .m_ready(m_ready),
      .overflow(overflow)
  );

endmodule
