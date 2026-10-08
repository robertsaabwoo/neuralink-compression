// Gate-level runs only (test/core): dump nlc_core's top-level nets (not the cell
// internals) for switching-activity based power (scripts/flow/flow.py).
//   PLUSARGS=+vcd=<file>                 dump from time 0
//   PLUSARGS="+vcd=<file> +vcd_start=<ns>"   start later (skip start-up, docs/testing.md 4.4)
`timescale 1ns / 1ps
module gl_dump;
  reg [8*512-1:0] vcd_file;
  reg [63:0]      vcd_start;
  initial begin
    if ($value$plusargs("vcd=%s", vcd_file)) begin
      if (!$value$plusargs("vcd_start=%d", vcd_start)) vcd_start = 0;
      #(vcd_start);
      $dumpfile(vcd_file);
      $dumpvars(1, nlc_core);
    end
  end
endmodule
