`default_nettype none
`timescale 1ns / 1ps

// nlc_lift53: one level of the streaming LeGall 5/3 integer lifting DWT
// (combinational; the 3 state registers per channel live in nlc_lossy).
//
//   d[i] = s[2i+1] - ((s[2i] + s[2i+2]) >> 1)
//   a[i] = s[2i]   + ((d[i-1] + d[i] + 2) >> 2)     d[-1] = d[0], s[n] = s[n-2]
//
// Input idx is the sample's index in the block at this level (n = 2^N_W
// samples). The pair (a[p], d[p]) is complete when s[2p+2] arrives, or on
// the last (odd) sample, which mirrors s[n] -> s[n-2].
module nlc_lift53 #(
    parameter int W   = 10,       // input width (signed); outputs are W+1
    parameter int N_W = 6         // log2(samples per block at this level)
) (
    input  logic                  v,
    input  logic [N_W-1:0]        idx,
    input  logic signed [W-1:0]   x,
    input  logic signed [W-1:0]   e,      // s[2i]   (last even input)
    input  logic signed [W-1:0]   o,      // s[2i+1] (pending odd input)
    input  logic signed [W:0]     dp,     // d[i-1]
    output logic                  pv,     // pair complete
    output logic [N_W-2:0]        p,      // its index
    output logic signed [W:0]     a,
    output logic signed [W:0]     d,
    output logic                  e_we,
    output logic                  o_we,
    output logic                  dp_we
);
  logic odd;
  assign odd = idx[0];
  assign pv  = v && (odd ? &idx : idx != '0);
  assign p   = odd ? idx[N_W-1:1] : idx[N_W-1:1] - 1'b1;

  // Sign extension and arithmetic shifts are written out as bit slices: tools
  // disagree on the signedness of casts (Yosys vs Icarus gave different results).
  logic [W-1:0] e_nx, o_cur;
  logic [W:0]   s2;
  logic [W+2:0] t;
  logic [W:0]   d_prev;
  assign e_nx   = odd ? e : x;                                  // mirror on the last sample
  assign o_cur  = odd ? x : o;                                  // last sample is the odd one
  assign s2     = {e[W-1], e} + {e_nx[W-1], e_nx};
  assign d      = {o_cur[W-1], o_cur} - {s2[W], s2[W:1]};       // o - (s2 >>> 1)
  assign d_prev = (p == '0) ? d : dp;
  assign t      = {{2{d_prev[W]}}, d_prev} + {{2{d[W]}}, d} + (W+3)'(2);
  assign a      = {e[W-1], e} + t[W+2:2];                       // e + (t >>> 2)

  assign e_we  = v && !odd;
  assign o_we  = v && odd;
  assign dp_we = pv;
endmodule
