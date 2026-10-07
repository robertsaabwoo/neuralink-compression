`default_nettype none

// Tiny Tapeout top level: pins <-> nlc_core.
// Rename the module to tt_um_<github username>_nlc (must be unique on the shuttle)
// and update info.yaml and test/tb.v to match.
//
// Pin map:
//   ui_in[7:0]  in   sample[7:0], or a config byte while cfg_en = 1
//   uio[1:0]    in   sample[9:8]
//   uio[2]      in   s_strobe  rising edge = one ADC slot (or one config byte)
//   uio[3]      in   s_frame   high with the strobe of slot 0
//   uio[4]      in   m_ack     rising edge = host has taken the byte on uo_out
//   uio[5]      in   cfg_en    1: strobes carry config bytes, address then data
//   uio[6]      out  m_valid
//   uio[7]      out  m_last    the byte on uo_out ends a packet
//   uo_out[7:0] out  m_data
//
// Off chip, strobes are edge-triggered rather than cycle-exact valid/ready
// because the pad and mux round trip is ~20 ns (one cycle at 50 MHz). The
// host must hold s_strobe low for at least one clock between slots, and wait
// two clocks after an m_ack edge before sampling m_valid/m_data again.
module tt_um_nlc_compressor (
    input  wire [7:0] ui_in,
    output wire [7:0] uo_out,
    input  wire [7:0] uio_in,
    output wire [7:0] uio_out,
    output wire [7:0] uio_oe,
    input  wire       ena,
    input  wire       clk,
    input  wire       rst_n
);

  // register all inputs once at the pads
  reg [7:0] ui_q;
  reg [7:0] uio_q;
  reg       strobe_q;
  reg       ack_q;

  always @(posedge clk) begin
    if (!rst_n) begin
      ui_q     <= 8'd0;
      uio_q    <= 8'd0;
      strobe_q <= 1'b0;
      ack_q    <= 1'b0;
    end else begin
      ui_q     <= ui_in;
      uio_q    <= uio_in;
      strobe_q <= uio_q[2];
      ack_q    <= uio_q[4];
    end
  end

  wire strobe = uio_q[2] & ~strobe_q;
  wire ack    = uio_q[4] & ~ack_q;
  wire cfg_en = uio_q[5];

  // config bytes: address, then data
  reg       have_addr;
  reg       cfg_we;
  reg [7:0] cfg_addr;
  reg [7:0] cfg_data;

  always @(posedge clk) begin
    if (!rst_n) begin
      have_addr <= 1'b0;
      cfg_we    <= 1'b0;
      cfg_addr  <= 8'd0;
      cfg_data  <= 8'd0;
    end else begin
      cfg_we <= 1'b0;
      if (!cfg_en) begin
        have_addr <= 1'b0;
      end else if (strobe) begin
        if (!have_addr) begin
          cfg_addr  <= ui_q;
          have_addr <= 1'b1;
        end else begin
          cfg_data  <= ui_q;
          cfg_we    <= 1'b1;
          have_addr <= 1'b0;
        end
      end
    end
  end

  wire [7:0] m_data;
  wire       m_valid;
  wire       m_last;
  wire       overflow;

  nlc_core core (
      .clk(clk), .rst_n(rst_n),
      .s_valid(strobe & ~cfg_en), .s_frame(uio_q[3]), .s_data({uio_q[1:0], ui_q}),
      .cfg_we(cfg_we), .cfg_addr(cfg_addr), .cfg_data(cfg_data),
      .m_data(m_data), .m_valid(m_valid), .m_last(m_last), .m_ready(ack),
      .overflow(overflow)
  );

  assign uo_out  = m_data;
  assign uio_out = {m_last, m_valid, 6'b0};
  assign uio_oe  = 8'b1100_0000;

  wire _unused = &{ena, uio_q[7:6], overflow, 1'b0};

endmodule
