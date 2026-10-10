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
    output wire                 s_want,     // the next s_valid (no s_frame) is a hit
    output reg                  smp_valid,
    output wire [ADC_BITS-1:0]  smp_data,   // smp_data .. smp_last: valid with smp_valid
    output wire [SEL_W-1:0]     smp_ch,
    output wire                 smp_first,  // channel 0 of a frame
    output wire                 smp_last,   // channel n_sel-1 of a frame
    output reg                  smp_tick,   // a new frame started (s_frame), not the first
    output reg                  smp_short   // with smp_tick: the frame that ended was short
);

  reg             running;
  reg [7:0]       slot_cnt;  // slot index of the next sample
  reg [SEL_W:0]   sel_idx;   // next selected channel to look for

  wire [7:0]      slot = s_frame ? 8'd0 : slot_cnt;
  wire [SEL_W:0]  idx  = s_frame ? {(SEL_W+1){1'b0}} : sel_idx;
  // the slot wanted next, on registered state only: lets the pads load the sample only when
  // it will be taken (frame strobes load regardless). On s_frame idx is 0: one 8:1 mux.
  wire [7:0]      want_n = sel_slots[8*sel_idx[SEL_W-1:0] +: 8];
  wire [7:0]      want = s_frame ? sel_slots[7:0] : want_n;
  wire            hit  = (running | s_frame) && (idx < n_sel) && (slot == want);
  assign s_want = running && (sel_idx < n_sel) && (slot_cnt == want_n);

  // Clock gating: while disabled (enable = 0) the block is cleared asynchronously and
  // sees no clock edge. Enabled, only the slot counter runs on every slot (gclk_s); the
  // selection state and the sample register change on a hit or s_frame only (gclk_h).
  wire clr_n = rst_n && enable;
  wire take  = s_valid && (hit || s_frame);
  wire gclk_s, gclk_h;
  nlc_icg u_cg_s (.clk(clk), .en(clr_n && s_valid),              .gclk(gclk_s));
  nlc_icg u_cg_h (.clk(clk), .en(clr_n && (take || smp_valid || smp_tick)), .gclk(gclk_h));

  nlc_greg #(.W(ADC_BITS + SEL_W + 2)) u_smp (
      .clk(gclk_h), .en(clr_n && s_valid && hit),
      .d({s_data, idx[SEL_W-1:0], idx == 0, idx + 1'b1 == n_sel}),
      .q({smp_data, smp_ch, smp_first, smp_last}));

  always @(posedge gclk_s or negedge clr_n) begin
    if (!clr_n) slot_cnt <= 8'd0;
    else        slot_cnt <= slot + 8'd1;              // s_valid
  end

  always @(posedge gclk_h or negedge clr_n) begin
    if (!clr_n) begin
      running   <= 1'b0;
      sel_idx   <= 0;
      smp_valid <= 1'b0;
      smp_tick  <= 1'b0;
      smp_short <= 1'b0;
    end else begin                                    // take, smp_valid or smp_tick
      smp_valid <= take && hit;
      smp_tick  <= take && s_frame && running;
      smp_short <= sel_idx != n_sel;                  // D5: channels of the frame not reached
      if (take) begin
        if (s_frame) running <= 1'b1;
        sel_idx <= hit ? idx + 1'b1 : idx;
      end
    end
  end

endmodule
