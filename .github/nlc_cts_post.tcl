# nlc area experiment G (not TT-standard): sourced by the patched LibreLane CTS script
# (scripts/ci/librelane_no_insertion_delay.sh) right after clock_tree_synthesis.
#
# OpenROAD builds a tree of at least three buffers (root + two leaves) on every clock-gate
# output, even when the gate drives a dozen flops in one spot. Below a gate that drives
# only flops, and at most NLC_CTS_FLAT_MAX of them, those buffers are removed: the gate
# (dlclkp) drives its flops directly. Measured on a preview of this design: 436 buffers
# (~2.2k um^2) removed, worst gated-clock slew 0.69 ns at ss (limit 0.75), hold still met.
set nlc_flat_max 16
if { [info exists ::env(NLC_CTS_FLAT_MAX)] } { set nlc_flat_max $::env(NLC_CTS_FLAT_MAX) }

set nlc_block [ord::get_db_block]
set nlc_rm {}
set nlc_nets 0
foreach nlc_inst [$nlc_block getInsts] {
  if { ![string match "*__dlclkp_*" [[$nlc_inst getMaster] getName]] } { continue }
  set nlc_out [$nlc_inst findITerm GCLK]
  if { $nlc_out eq "NULL" } { continue }
  set nlc_net [$nlc_out getNet]
  if { $nlc_net eq "NULL" } { continue }
  set nlc_todo [list $nlc_net]
  set nlc_mine {}
  set nlc_flops 0
  set nlc_other 0
  while { [llength $nlc_todo] } {
    set n [lindex $nlc_todo 0]
    set nlc_todo [lrange $nlc_todo 1 end]
    foreach it [$n getITerms] {
      if { [$it isOutputSignal] } { continue }
      set i2 [$it getInst]
      set m [[$i2 getMaster] getName]
      if { [string match "*__clkbuf_*" $m] } {
        lappend nlc_mine [$i2 getName]
        set o [$i2 findITerm X]
        if { $o ne "NULL" && [$o getNet] ne "NULL" } { lappend nlc_todo [$o getNet] }
      } elseif { [string match "*__df*" $m] } {
        incr nlc_flops
      } else {
        incr nlc_other
      }
    }
  }
  if { $nlc_flops > 0 && $nlc_flops <= $nlc_flat_max && $nlc_other == 0 && [llength $nlc_mine] } {
    set nlc_rm [concat $nlc_rm $nlc_mine]
    incr nlc_nets
  }
}
puts "\[INFO\] nlc: removing [llength $nlc_rm] CTS buffers below $nlc_nets clock gates (<= $nlc_flat_max flops each)"
if { [llength $nlc_rm] } {
  remove_buffers {*}[lmap b $nlc_rm { get_cells $b }]
}
