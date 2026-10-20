# CircuitLoop non-project Vivado flow (batch mode, no GUI).
#
#   vivado -mode batch -nojournal -notrace -log vivado.log -source build.tcl -tclargs CLBUILD_<id>
#
# Reads config.tcl from the same directory, which defines:
#   CL_PART CL_TOP CL_SOURCES CL_XDC CL_THREADS CL_BIT_NAME
# Emits "CL_STAGE <stage> <running|passed|failed>" markers on stdout and writes
# results.json (status, failed stage, message, metrics) on success and failure.

set cl_build_tag [lindex $argv 0]
cd [file dirname [file normalize [info script]]]
source config.tcl

array set cl_metrics {}

proc cl_json_escape {s} {
    return [string map [list "\\" "\\\\" "\"" "\\\"" "\n" "\\n" "\r" "" "\t" " "] $s]
}

proc cl_write_result {status stage message} {
    global cl_metrics
    set fh [open results.json w]
    puts $fh "\{"
    puts $fh "  \"status\": \"$status\","
    puts $fh "  \"failed_stage\": \"$stage\","
    puts $fh "  \"message\": \"[cl_json_escape $message]\","
    puts $fh "  \"vivado_version\": \"[cl_json_escape [version -short]]\","
    set items {}
    foreach k [lsort [array names cl_metrics]] {
        lappend items "    \"$k\": \"[cl_json_escape $cl_metrics($k)]\""
    }
    puts $fh "  \"metrics\": \{"
    puts $fh [join $items ",\n"]
    puts $fh "  \}"
    puts $fh "\}"
    close $fh
}

# Record a metric; a failing query records "NA" instead of failing the build.
proc cl_metric {name body} {
    global cl_metrics
    if {[catch {uplevel 1 $body} value]} {
        set cl_metrics($name) "NA"
    } else {
        set cl_metrics($name) $value
    }
}

proc cl_stage {name status} {
    puts "CL_STAGE $name $status"
    flush stdout
}

proc cl_run {stage body} {
    cl_stage $stage running
    if {[catch {uplevel 1 $body} err]} {
        cl_stage $stage failed
        puts "CL_ERROR $stage [string map {"\n" " | "} $err]"
        flush stdout
        cl_write_result failed $stage $err
        exit 2
    }
    cl_stage $stage passed
}

set_param general.maxThreads $CL_THREADS
file mkdir reports
file mkdir bitstream

cl_run read {
    foreach f $CL_SOURCES {
        read_verilog $f
    }
    read_xdc $CL_XDC
}

cl_run synth {
    synth_design -top $CL_TOP -part $CL_PART
    report_utilization -file reports/utilization_synth.rpt
    cl_metric synth_critical_warnings { get_msg_config -severity {CRITICAL WARNING} -count }
}

cl_run opt {
    opt_design
}

cl_run place {
    place_design
}

cl_run route {
    route_design
    write_checkpoint -force post_route.dcp
}

cl_run reports {
    report_timing_summary -max_paths 10 -report_unconstrained -file reports/timing_summary.rpt
    report_utilization -file reports/utilization_impl.rpt
    report_utilization -hierarchical -file reports/utilization_hier.rpt
    report_drc -file reports/drc.rpt
    report_io -file reports/io.rpt
    report_power -file reports/power.rpt
    cl_metric wns_ns { get_property SLACK [get_timing_paths -max_paths 1 -nworst 1 -setup] }
    cl_metric whs_ns { get_property SLACK [get_timing_paths -max_paths 1 -nworst 1 -hold] }
    cl_metric unrouted_nets { llength [get_nets -quiet -hierarchical -filter {ROUTE_STATUS == UNROUTED}] }
    cl_metric ports_total { llength [get_ports] }
    cl_metric ports_without_loc { llength [get_ports -quiet -filter {IS_LOC_FIXED == 0}] }
    cl_metric critical_warnings { get_msg_config -severity {CRITICAL WARNING} -count }
    cl_metric errors { get_msg_config -severity {ERROR} -count }
}

cl_run bitstream {
    write_bitstream -force bitstream/$CL_BIT_NAME
}

cl_write_result passed "" ""
cl_stage done passed
exit 0
