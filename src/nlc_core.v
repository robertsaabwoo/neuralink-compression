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
    // configuration write port
    input  wire                 cfg_we,
    input  wire [7:0]           cfg_addr,
    input  wire [7:0]           cfg_data,
    // packet byte stream
    output wire [7:0]           m_data,
    output wire                 m_valid,
    output wire                 m_last,
    input  wire                 m_ready,
    output wire                 overflow
);

  localparam SEL_W   = $clog2(N_SEL);
  localparam FIFO_AW = $clog2(FIFO_DEPTH);

  wire [1:0]          mode;
  wire                enable;
  wire [SEL_W:0]      n_sel;
  wire [7:0]          fpp;
  wire [15:0]         win_len;
  wire [4:0]          sbp_shift;
  wire [8*N_SEL-1:0]  sel_slots;
  wire [8*N_SEL-1:0]  thr;

  nlc_cfg #(.N_SEL(N_SEL)) u_cfg (
      .clk(clk), .rst_n(rst_n),
      .cfg_we(cfg_we), .cfg_addr(cfg_addr), .cfg_data(cfg_data),
      .mode(mode), .enable(enable), .n_sel(n_sel), .fpp(fpp),
      .win_len(win_len), .sbp_shift(sbp_shift), .sel_slots(sel_slots), .thr(thr)
  );

  wire                smp_valid;
  wire [ADC_BITS-1:0] smp_data;
  wire [SEL_W-1:0]    smp_ch;
  wire                smp_first;
  wire                smp_last;

  nlc_slot_sel #(.ADC_BITS(ADC_BITS), .N_SEL(N_SEL)) u_sel (
      .clk(clk), .rst_n(rst_n), .enable(enable), .n_sel(n_sel), .sel_slots(sel_slots),
      .s_valid(s_valid), .s_frame(s_frame), .s_data(s_data),
      .smp_valid(smp_valid), .smp_data(smp_data), .smp_ch(smp_ch),
      .smp_first(smp_first), .smp_last(smp_last)
  );

  wire                wr_en;
  wire [7:0]          wr_data;
  wire                wr_last;
  wire                fifo_full;
  wire [FIFO_AW:0]    fifo_count;

  nlc_encoder #(.ADC_BITS(ADC_BITS), .N_SEL(N_SEL), .FIFO_AW(FIFO_AW)) u_enc (
      .clk(clk), .rst_n(rst_n),
      .enable(enable), .mode(mode), .n_sel(n_sel), .fpp(fpp), .win_len(win_len),
      .sbp_shift(sbp_shift), .thr(thr),
      .smp_valid(smp_valid), .smp_data(smp_data), .smp_ch(smp_ch),
      .smp_first(smp_first), .smp_last(smp_last),
      .wr_en(wr_en), .wr_data(wr_data), .wr_last(wr_last),
      .fifo_full(fifo_full), .fifo_count(fifo_count)
  );

  nlc_out_fifo #(.DEPTH(FIFO_DEPTH)) u_fifo (
      .clk(clk), .rst_n(rst_n),
      .wr_en(wr_en), .wr_data(wr_data), .wr_last(wr_last),
      .full(fifo_full), .count(fifo_count),
      .m_valid(m_valid), .m_data(m_data), .m_last(m_last), .m_ready(m_ready),
      .overflow(overflow)
  );

endmodule
