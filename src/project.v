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
//   uio[4]      in   unused (was m_ack; D8: the output has no back-pressure)
//   uio[5]      in   cfg_en    1: strobes carry config bytes, address then data
//   uio[6]      out  m_valid   uo_out holds a byte in this clock
//   uio[7]      out  m_last    with m_valid: the byte on uo_out ends a packet
//                              m_last = 1 with m_valid = 0: abort token (D5/D7), the
//                              host drops its partial packet
//   uo_out[7:0] out  m_data
//
// Output (decision D8, valid-only streaming): one byte per clock while m_valid, the abort
// token for one clock; the host must capture every clock (nothing can stall the output,
// like the merge circuitry / serializer of an implant). At the real frame rate the coder
// can never fall a frame behind for any data (scripts/proofs/output_bound.py), so the
// output never drops a packet; only a short frame (D5) or enable falling (D7) aborts one.
//
// Off chip, strobes are edge-triggered rather than cycle-exact valid/ready
// because the pad and mux round trip is ~20 ns (one cycle at 50 MHz). The
// host must hold s_strobe low for at least one clock between slots.
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

  // Inputs are registered once at the pads, in three clock groups (power: the TT pins are
  // the only registers that would otherwise clock every cycle, docs/results.md F19/F20):
  //   every clock     the strobe and cfg_en levels and the edge detectors
  //   every strobe    the frame flag (the slot selector reads it on every slot)
  //   selected slots  the sample / config byte: read only on a hit or a config write, so it
  //                   loads only on a strobe of a selected slot (s_want), a frame strobe or
  //                   a config strobe. s_want comes from registered state of the slot
  //                   selector, valid for the next strobe (>= 2 clocks per slot, C-IF-9).
  // CTS pads registers that share a clock net with clock gates (F19), so every group has
  // its own gate (the first one always on).
  reg       stb_q, cfgen_q;           // pin levels: s_strobe, cfg_en
  reg       strobe_q;                 // previous strobe level (edge detect)
  reg       frame_q;                  // s_frame
  reg [7:0] ui_q;                     // sample[7:0] / config byte
  reg [1:0] hi_q;                     // sample[9:8]
  wire      s_want;                   // from the core: the next slot strobe is selected

  wire ld_frame = !rst_n || uio_in[2];
  wire ld_data  = !rst_n || (uio_in[2] && (uio_in[5] || uio_in[3] || s_want));
  wire clk_pins, clk_frame, clk_data;
  nlc_icg u_cg_pins (.clk(clk), .en(1'b1),     .gclk(clk_pins));
  nlc_icg u_cg_frm  (.clk(clk), .en(ld_frame), .gclk(clk_frame));
  nlc_icg u_cg_dat  (.clk(clk), .en(ld_data),  .gclk(clk_data));

  always @(posedge clk_pins) begin
    if (!rst_n) begin
      stb_q    <= 1'b0;
      cfgen_q  <= 1'b0;
      strobe_q <= 1'b0;
    end else begin
      stb_q    <= uio_in[2];
      cfgen_q  <= uio_in[5];
      strobe_q <= stb_q;
    end
  end

  always @(posedge clk_frame) begin
    if (!rst_n) frame_q <= 1'b0;
    else        frame_q <= uio_in[3];
  end

  always @(posedge clk_data) begin
    if (!rst_n) begin
      ui_q <= 8'd0;
      hi_q <= 2'd0;
    end else begin
      ui_q <= ui_in;
      hi_q <= uio_in[1:0];
    end
  end

  wire strobe = stb_q & ~strobe_q;
  wire cfg_en = cfgen_q;

  // config bytes: address, then data. cfg_addr/cfg_data are clock-gated (written on a
  // config strobe only; read only with cfg_we, so they need no reset).
  reg        have_addr;
  reg        cfg_we;
  wire [7:0] cfg_addr;
  wire [7:0] cfg_data;
  wire       cfg_strobe = rst_n & cfg_en & strobe;

  nlc_greg #(.W(8)) u_cfg_addr (.clk(clk), .en(cfg_strobe & ~have_addr), .d(ui_q), .q(cfg_addr));
  nlc_greg #(.W(8)) u_cfg_data (.clk(clk), .en(cfg_strobe &  have_addr), .d(ui_q), .q(cfg_data));

  always @(posedge clk_pins) begin
    if (!rst_n) begin
      have_addr <= 1'b0;
      cfg_we    <= 1'b0;
    end else begin
      cfg_we <= 1'b0;
      if (!cfg_en) begin
        have_addr <= 1'b0;
      end else if (strobe) begin
        have_addr <= ~have_addr;
        cfg_we    <= have_addr;
      end
    end
  end

  wire [7:0] m_data;
  wire       m_valid;
  wire       m_last;
  wire       m_abort;
  wire       overflow;

  nlc_core core (
      .clk(clk), .rst_n(rst_n),
      .s_valid(strobe & ~cfg_en), .s_frame(frame_q), .s_data({hi_q, ui_q}), .s_want(s_want),
      .cfg_we(cfg_we), .cfg_addr(cfg_addr), .cfg_data(cfg_data),
      .m_data(m_data), .m_valid(m_valid), .m_last(m_last), .m_abort(m_abort),
      .overflow(overflow)
  );

  assign uo_out  = m_data;
  assign uio_out = {m_abort | m_last, m_valid, 6'b0};   // core: m_last implies m_valid
  assign uio_oe  = 8'b1100_0000;

  wire _unused = &{ena, uio_in[7:6], uio_in[4], overflow, 1'b0};

endmodule
