`default_nettype none

// Harness self-test stand-in for src/nlc_encoder.v (make ENCODER=replay).
// It does no compression: it replays the golden model's expected bytes for
// the configured mode into the FIFO, so the pins, config path, slot selector,
// FIFO, back-pressure and the cocotb checker can be verified without a real
// encoder. If a test passes here but fails with your encoder, the bug is in
// your encoder.
module nlc_encoder #(
    parameter ADC_BITS = 10,
    parameter N_SEL    = 8,
    parameter SEL_W    = $clog2(N_SEL),
    parameter FIFO_AW  = 3
) (
    input  wire                 clk,
    input  wire                 rst_n,
    input  wire                 enable,
    input  wire [1:0]           mode,
    input  wire [SEL_W:0]       n_sel,
    input  wire [7:0]           fpp,
    input  wire [15:0]          win_len,
    input  wire [4:0]           sbp_shift,
    input  wire [8*N_SEL-1:0]   thr,
    input  wire                 smp_valid,
    input  wire [ADC_BITS-1:0]  smp_data,
    input  wire [SEL_W-1:0]     smp_ch,
    input  wire                 smp_first,
    input  wire                 smp_last,
    output wire                 wr_en,
    output wire [7:0]           wr_data,
    output wire                 wr_last,
    input  wire                 fifo_full,
    input  wire [FIFO_AW:0]     fifo_count
);

  localparam MAXB = 8192;

  // entries: {end_marker, last, byte}; gen_vectors.py writes a 0x200 terminator
  reg [9:0] mem0 [0:MAXB-1];
  reg [9:0] mem1 [0:MAXB-1];
  reg [9:0] mem2 [0:MAXB-1];
  reg [9:0] mem3 [0:MAXB-1];
  initial begin
    $readmemh("vectors/lossless/expected.hex", mem0);
    $readmemh("vectors/lossy/expected.hex", mem1);
    $readmemh("vectors/binned/expected.hex", mem2);
    $readmemh("vectors/sbp/expected.hex", mem3);
  end

  reg [12:0] idx;
  reg        started;
  wire [9:0] cur = (mode == 2'd0) ? mem0[idx] : (mode == 2'd1) ? mem1[idx] :
                   (mode == 2'd2) ? mem2[idx] : mem3[idx];

  always @(posedge clk) begin
    if (!rst_n || !enable) begin
      idx     <= 0;
      started <= 1'b0;
    end else begin
      if (smp_valid) started <= 1'b1;
      if (wr_en) idx <= idx + 1'b1;
    end
  end

  assign wr_en   = started && !fifo_full && (cur[9] === 1'b0);
  assign wr_data = cur[7:0];
  assign wr_last = cur[8];

endmodule
