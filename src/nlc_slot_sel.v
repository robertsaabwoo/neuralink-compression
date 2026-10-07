`default_nettype none

// ADC slot stream -> selected-channel sample stream.
//
// The ADC mux delivers one 10-bit sample per slot, slot 0 flagged by s_frame,
// with no backpressure. This block counts slots, picks out the n_sel
// configured slots (which must be strictly ascending), and emits them as
// channel 0..n_sel-1. Nothing is emitted until the first s_frame after
// enable, so packet 0 always starts on a frame boundary.
module nlc_slot_sel #(
    parameter ADC_BITS = 10,
    parameter N_SEL    = 8,
    parameter SEL_W    = $clog2(N_SEL)
) (
    input  wire                 clk,
    input  wire                 rst_n,
    input  wire                 enable,
    input  wire [SEL_W:0]       n_sel,
    input  wire [8*N_SEL-1:0]   sel_slots,
    input  wire                 s_valid,
    input  wire                 s_frame,
    input  wire [ADC_BITS-1:0]  s_data,
    output reg                  smp_valid,
    output reg  [ADC_BITS-1:0]  smp_data,
    output reg  [SEL_W-1:0]     smp_ch,
    output reg                  smp_first,  // channel 0 of a frame
    output reg                  smp_last    // channel n_sel-1 of a frame
);

  reg             running;
  reg [7:0]       slot_cnt;  // slot index of the next sample
  reg [SEL_W:0]   sel_idx;   // next selected channel to look for

  wire [7:0]      slot = s_frame ? 8'd0 : slot_cnt;
  wire [SEL_W:0]  idx  = s_frame ? {(SEL_W+1){1'b0}} : sel_idx;
  wire [7:0]      want = sel_slots[8*idx[SEL_W-1:0] +: 8];
  wire            hit  = (running | s_frame) && (idx < n_sel) && (slot == want);

  always @(posedge clk) begin
    if (!rst_n || !enable) begin
      running   <= 1'b0;
      slot_cnt  <= 8'd0;
      sel_idx   <= 0;
      smp_valid <= 1'b0;
      smp_data  <= 0;
      smp_ch    <= 0;
      smp_first <= 1'b0;
      smp_last  <= 1'b0;
    end else begin
      smp_valid <= 1'b0;
      if (s_valid) begin
        if (s_frame) running <= 1'b1;
        slot_cnt <= slot + 8'd1;
        sel_idx  <= hit ? idx + 1'b1 : idx;
        if (hit) begin
          smp_valid <= 1'b1;
          smp_data  <= s_data;
          smp_ch    <= idx[SEL_W-1:0];
          smp_first <= (idx == 0);
          smp_last  <= (idx + 1'b1 == n_sel);
        end
      end
    end
  end

endmodule
