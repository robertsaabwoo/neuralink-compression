`default_nettype none

// Harness self-test stand-in for src/nlc_encoder.v (make ENCODER=replay).
// It does no compression: it replays the golden model's expected lossy bytes
// as the output stream (one byte per clock, D8), so the pins, config path, slot
// selector and the cocotb checker can be verified without the real encoder. If a test
// passes here but fails with the real encoder, the bug is in the encoder.
module nlc_encoder #(
    parameter ADC_BITS = 10,
    parameter N_SEL    = 8,
    parameter SEL_W    = $clog2(N_SEL)
) (
    input  wire                 clk,
    input  wire                 rst_n,
    input  wire                 enable,
    input  wire [SEL_W:0]       n_sel,
    input  wire                 smp_valid,
    input  wire [ADC_BITS-1:0]  smp_data,
    input  wire [SEL_W-1:0]     smp_ch,
    input  wire                 smp_first,
    input  wire                 smp_last,
    input  wire                 smp_tick,
    input  wire                 smp_short,
    output wire                 m_valid,
    output wire [7:0]           m_data,
    output wire                 m_last,
    output wire                 m_abort,
    output wire                 overflow,
    output wire                 busy
);

  localparam MAXB = 8192;

  // entries: {end_marker, last, byte}; gen_vectors.py writes a 0x200 terminator
  reg [9:0] mem [0:MAXB-1];
  initial $readmemh("vectors/lossy/expected.hex", mem);

  reg [12:0] idx;
  reg        started;
  wire [9:0] cur = mem[idx];

  always @(posedge clk) begin
    if (!rst_n || !enable) begin
      idx     <= 0;
      started <= 1'b0;
    end else begin
      if (smp_valid) started <= 1'b1;
      if (m_valid) idx <= idx + 1'b1;
    end
  end

  assign m_valid  = started && (cur[9] === 1'b0);
  assign m_data   = cur[7:0];
  assign m_last   = m_valid && cur[8];
  assign m_abort  = 1'b0;
  assign overflow = 1'b0;

  assign busy = 1'b1;                        // replay: keep the core clocked

endmodule
