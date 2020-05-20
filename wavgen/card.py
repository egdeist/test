## For Card Control ##
from spectrum import *
## For Cam Control ##
from instrumental import instrument, u
import matplotlib.animation as animation
from matplotlib.widgets import Button, Slider
## Submodules ##
from .helper import fix_exposure, analyze_image, plot_image
from .step import Step
## Other ##
from math import log2, ceil, sqrt
import sys
from time import time, sleep
import easygui
import matplotlib.pyplot as plt
import numpy as np
import warnings


## Warning Suppression ##
warnings.filterwarnings("ignore", category=FutureWarning, module="instrumental")

### Parameter ###
SAMP_FREQ = 1000E6  #: Modify if a different Sampling Frequency is required.
NUMPY_MAX = int(1E5)  #: Max size of Software buffer for board transfers (in samples)
MAX_EXP = 150
""" Maximum value for Thorcam exposure """


# noinspection PyTypeChecker,PyUnusedLocal,PyProtectedMember
class Card:
    """ Class designed for Opening, Configuring, running the Spectrum AWG card.

    ATTRIBUTES
    ----------
    hCard
        The handle to the open card. For use with Spectrum API functions.
    ModeBook : dict[str, int]
        Dictionary for retrieving board register constants from key phrases.
    ModeReady, ChanReady, BufReady : Bool
        Indicators of card configuration completion.
    Waveforms : list of :obj:`Waveform`

    Methods
    -------
    set_mode(mode)
        Set the card operation mode, e.g. multiple, continuous.
    setup_channels(amplitude, ch0, ch1, filter)
        Activates chosen channels and Configures Triggers.
    setup_buffer()
        Transfers the waveform to Board Memory.
    load_segments(segs)
        Appends a set of segments to the current set.
    clear_segments()
        Clears out current set of Segments.
    reset_card()
        Resets all of the cards configuration. Doesn't close card.

    Private Methods
    ---------------
    _error_check()
        Reads the card's error register.
    _load(seg, Ptr, buf, fsamp)
        Computes a Segment and Transfers to Card.
    """
    hCard = None
    ModeBook = {
        'continuous': SPC_REP_STD_CONTINUOUS,
        # 'multi'     : SPC_REP_STD_MULTI,
        'sequential': SPC_REP_STD_SEQUENCE
    }  #: Dictionary of Mode Names to Register Value Constants

    def __init__(self, mode='continuous'):
        """ Just Opens the card in the given mode.

            Parameters
            ----------
            mode : {'continuous', 'sequential'}
                Card operational mode.
        """
        assert self.hCard is None, "Card opened twice!"

        self.hCard = spcm_hOpen(create_string_buffer(b'/dev/spcm0'))
        """Handle to card device. *Class object* See `spectrum.pyspcm.py`"""
        self._error_check()
        self.ModeReady = True  #: bool : Indicates a proper card mode is set.
        self.ChanReady = False  #: bool : Indicates channels are setup.
        self.BufReady = False  #: bool : Indicates the card buffer is configured & loaded with waveform data.
        self.ProgrammedSequence = False if mode == 'sequential' else True  #: bool : Indicates a sequence is programmed.
        self.Mode = mode  #: str : The current operation mode of the car.
        self.Waveforms = None  #: list of :obj:`Waveform` : List of Waveform or inheriting subclass objects.

        spcm_dwSetParam_i32(self.hCard, SPC_M2CMD, M2CMD_CARD_RESET)  # Clears the card's configuration

        ## Setup Mode ##
        mode = self.ModeBook.get(mode)  # ModeBook is class object, look above
        if mode is None:
            print('Invalid mode phrase, possible phrases are: ')
            print(list(self.ModeBook.keys()))
            exit(1)

        spcm_dwSetParam_i32(self.hCard, SPC_CARDMODE, mode)  # Writes the mode to card

        if self.Mode is 'continuous':  # Sets the card to loop continuously
            spcm_dwSetParam_i64(self.hCard, SPC_LOOPS, int64(0))
        self._error_check()
        self.ModeReady = True

    def __exit__(self, exception_type, exception_value, traceback):
        print("in __exit__")
        spcm_vClose(self.hCard)

    ################# PUBLIC FUNCTIONS #################

    #### Segment Object Handling ####
    def load_waveforms(self, wavs):
        """ Passes a waveforms to the card.
        """
        self.Waveforms = wavs

    def setup_channels(self, amplitude=200, ch0=False, ch1=True, use_filter=False):
        """ Performs a Standard Initialization for designated Channels & Trigger.

        INPUTS:
        -------
            amplitude -- Sets the Output Amplitude ~~ RANGE: [80 - 2000](mV) inclusive
            ch0 -------- Bool to Activate Channel0
            ch1 -------- Bool to Activate Channel1
            use_filter - Bool to Activate Output Filter
        """
        ## Input Validation ##
        if ch0 and ch1:
            print('Multi-Channel Support Not Yet Supported!')
            print('Defaulting to Ch1 only.')
            ch0 = False

        assert 80 <= amplitude <= 240, "Amplitude must within interval: [80 - 2000]"
        if amplitude != int(amplitude):
            amplitude = int(amplitude)
            print("Rounding amplitude to required integer value: ", amplitude)

        ## Channel Activation ##
        CHAN = 0x00000000
        amp = int32(amplitude)
        if ch0:
            spcm_dwSetParam_i32(self.hCard, SPC_ENABLEOUT0, 1)
            CHAN = CHAN ^ CHANNEL0
            spcm_dwSetParam_i32(self.hCard, SPC_AMP0,       amp)
            spcm_dwSetParam_i64(self.hCard, SPC_FILTER0,    int64(use_filter))
        if ch1:
            spcm_dwSetParam_i32(self.hCard, SPC_ENABLEOUT1, 1)
            CHAN = CHAN ^ CHANNEL1
            spcm_dwSetParam_i32(self.hCard, SPC_AMP1,       amp)
            spcm_dwSetParam_i64(self.hCard, SPC_FILTER1,    int64(use_filter))
        spcm_dwSetParam_i32(self.hCard, SPC_CHENABLE,       CHAN)

        ## Trigger Config ##
        spcm_dwSetParam_i32(self.hCard, SPC_TRIG_ORMASK,    SPC_TMASK_SOFTWARE)
        ## Necessary? Doesn't Hurt ##
        spcm_dwSetParam_i32(self.hCard, SPC_TRIG_ANDMASK,   0)
        spcm_dwSetParam_i64(self.hCard, SPC_TRIG_DELAY,     int64(0))
        spcm_dwSetParam_i32(self.hCard, SPC_TRIGGEROUT,     0)
        ############ ???? ###########
        self._error_check()
        self.ChanReady = True

    def setup_buffer(self, verbose=False):
        """ Calculates waves, configures card memory, then transfers to card.

        Ought to be Fool-Proofed eventually, presently it can be broken, but only if you try ;)
        """
        ## Validate ##
        assert self.ChanReady and self.ModeReady, "The Mode & Channels must be configured before Buffer!"
        assert len(self.Waveforms) > 0, "No Waveforms defined! Nothing to put in Buffer."

        ## Gather Information from Board ##
        num_chan = int32(0)  # Number of Open Channels
        mem_size = uint64(0)  # Total Memory ~ 4.3 GB
        spcm_dwGetParam_i32(self.hCard, SPC_CHCOUNT,    byref(num_chan))
        spcm_dwGetParam_i64(self.hCard, SPC_PCIMEMSIZE, byref(mem_size))

        ## Configures Memory Size & Divisions ##
        if self.Mode == 'continuous':
            ## Define the Size of required Board Memory ##
            buf_size = self.Waveforms[0].SampleLength*2*num_chan.value
            spcm_dwSetParam_i64(self.hCard, SPC_MEMSIZE, int64(self.Waveforms[0].SampleLength))

            ## Sets up a local Software Buffer then Transfers to Board ##
            pv_buf = pvAllocMemPageAligned(buf_size)  # Allocates space on PC
            pn_buf = cast(pv_buf, ptr16)  # Casts pointer into something usable
            self.Waveforms[0].load(pn_buf, 0, buf_size)

            ## Do a Transfer ##
            spcm_dwDefTransfer_i64(self.hCard, SPCM_BUF_DATA, SPCM_DIR_PCTOCARD, 0, pv_buf, uint64(0), uint64(buf_size))
            if verbose:
                print("Doing a transfer...%d bytes" % buf_size)
            spcm_dwSetParam_i32(self.hCard, SPC_M2CMD, M2CMD_DATA_STARTDMA | M2CMD_DATA_WAITDMA)
            if verbose:
                print("Done")
        else:
            self._setup_sequential_buffer(mem_size, num_chan, verbose)

        ## Setup the Clock & Wrap Up ##
        self._setup_clock(verbose)
        self._error_check()
        self.BufReady = True

    def wiggle_output(self, timeout=0, cam=None, verbose=False, stop=True):
        """ Performs a Standard Output for configured settings.

        Parameters
        ----------
        -- OPTIONAL --
        timeout : int
            How long the output streams in Milliseconds.
        cam : bool
            Indicates whether to use Camera GUI.
            `True` or `False` selects Pre- or Post- chamber cameras respectively.
        verbose : bool
            Verbosity control!
        stop : bool
            Stops the card on function exit?

        Returns
        -------
        None
            WAVES! (This function itself actually returns void)
        """
        if self.ChanReady and self.ModeReady and not self.BufReady:
            print("Psst..you need to reconfigure the buffer after switching modes.")
        assert self.BufReady and self.ChanReady and self.ModeReady, "Card not fully configured"
        assert self.ProgrammedSequence, "If your using 'sequential' mode, you must us 'load_sequence()'."

        WAIT = 0
        if self.Mode == 'continuous':
            if verbose:
                print("Looping Signal for ", timeout / 1000 if timeout else "infinity", " seconds...")
            if timeout != 0:
                WAIT = M2CMD_CARD_WAITREADY
            spcm_dwSetParam_i32(self.hCard, SPC_TIMEOUT, timeout)

        dwError = spcm_dwSetParam_i32(self.hCard, SPC_M2CMD, M2CMD_CARD_START | M2CMD_CARD_ENABLETRIGGER | WAIT)
        count = 0
        while dwError == ERR_CLOCKNOTLOCKED:
            count += 1
            sleep(0.1)
            self._error_check(halt=False, print_err=False)
            dwError = spcm_dwSetParam_i32(self.hCard, SPC_M2CMD, M2CMD_CARD_START | M2CMD_CARD_ENABLETRIGGER | WAIT)
            if count == 10:
                break

        if dwError == ERR_TIMEOUT:
            print("timeout!")
        elif cam is not None:
            self._run_cam(cam, verbose)
        elif timeout == 0 and stop:
            easygui.msgbox('Stop Card?', 'Infinite Looping!')

        if stop:
            spcm_dwSetParam_i32(self.hCard, SPC_M2CMD, M2CMD_CARD_STOP)
        self._error_check()

    def load_sequence(self, steps, verbose=False):
        """ Passes an :obj:`Sequence` to the card.
        """
        assert self.Mode == 'sequential', "Cannot load sequence unless in Sequential mode."
        for step in steps:
            cur = step.CurrentStep
            seg = step.SegmentIndex
            loop = step.Loops
            nxt = step.NextStep
            cond = step.Condition
            reg_upper = int32(cond | loop)
            reg_lower = int32(nxt << 16 | seg)
            if verbose:
                print("Step %.2d: 0x%08x_%08x\n" % (cur, reg_upper.value, reg_lower.value))
            spcm_dwSetParam_i64m(self.hCard, SPC_SEQMODE_STEPMEM0 + cur, reg_upper, reg_lower)
        self.ProgrammedSequence = True

        if verbose:
            print("\nDump!:\n")
            for i in range(len(steps)):
                temp = uint64(0)
                spcm_dwGetParam_i64(self.hCard, SPC_SEQMODE_STEPMEM0 + i, byref(temp))
                print("Step %.2d: 0x%08x_%08x\n" % (i, int32(temp.value >> 32).value, int32(temp.value).value))
                print("Also: %16x\n" % temp.value)

    def stabilize_intensity(self, which_cam, cam, verbose=False):
        """ Balances power across traps.

        Given a UC480 camera object (instrumental module) and
        a number indicating the number of trap objects,
        applies an iterative image analysis to individual trap adjustment
        in order to achieve a nearly homogeneous intensity profile across traps.

        Parameters
        ----------
        which_cam : bool
            `True` or `False` selects Pre- or Post- chamber cameras respectively.
        cam : :obj:`instrumental.drivers.cameras.uc480`
            The camera object opened by :obj:`instrumental` module.
        """
        L = 0.2  # Correction Rate
        mags = self.Waveforms[0].get_magnitudes()
        ntraps = len(mags)
        step_num, rel_dif = 0, 1
        while step_num < 5:
            step_num += 1
            print("Iteration ", step_num)

            trap_powers = analyze_image(which_cam, cam, ntraps, step_num)

            mean_power = trap_powers.mean()
            rel_dif = 100 * trap_powers.std() / mean_power
            print(f'Relative Power Difference: {rel_dif:.2f} %')

            if rel_dif < 0.1:
                print("WOW")
                break
            elif rel_dif < 0.36:
                L = 0.001
            elif rel_dif < 0.5:
                L = 0.01
            elif rel_dif < 2:
                L = 0.05
            elif rel_dif < 5:
                L = 0.1

            deltaM = [(mean_power - P)/P for P in trap_powers]
            dmags = [L * dM / sqrt(abs(dM)) for dM in deltaM]
            mags = np.add(mags, dmags)
            self._update_magnitudes(mags)

        for i in range(5):
            if rel_dif > 0.5:
                break
            sleep(2)

            # im = np.zeros(cam.latest_frame().shape)
            # for _ in range(10):
            #     imm = cam.latest_frame()
            #     for _ in range(9):
            #         imm = np.add(imm, cam.latest_frame())
            #     imm = np.multiply(imm, 0.1)
            #
            #     im = np.add(im, imm)
            # im = np.multiply(im, 0.1)

            trap_powers = analyze_image(which_cam, cam, ntraps)
            dif = 100 * trap_powers.std() / trap_powers.mean()
            print(f'Relative Power Difference: {dif:.2f} %')

        plot_image(which_cam, cam.latest_frame(), ntraps)

    def reset_card(self):
        """ Wipes Card Configuration clean.
        """
        spcm_dwSetParam_i32(self.hCard, SPC_M2CMD, M2CMD_CARD_RESET)
        self.ModeReady = False
        self.ChanReady = False
        self.BufReady = False

    ################# PRIVATE FUNCTIONS #################

    def _error_check(self, halt=True, print_err=True):
        """
        Checks the Error Register.

        If Occupied:
            -Prints Error
            -Optionally closes the Card and exits program
            -Or returns False
        Else:
            -Returns True
        """
        ErrBuf = create_string_buffer(ERRORTEXTLEN)  # Buffer for returned Error messages
        if spcm_dwGetErrorInfo_i32(self.hCard, None, None, ErrBuf) != ERR_OK:
            if print_err:
                sys.stdout.write("Warning: {0}".format(ErrBuf.value))
            if halt:
                spcm_vClose(self.hCard)
                exit(1)
            return False
        return True

    def _setup_sequential_buffer(self, mem_size, num_chan, verbose=False):
        """
        Figures how to chop up memory to accommodate each sequence step.

        Parameters
        ----------
        mem_size : uint64
            Size in bytes of card physical memory.
        num_chan : int32
            Number of active card channels.
        --OPTIONAL--
        verbose : bool
        """
        ## Checks if Waveforms can fit in Memory ##
        fracs = [2 * w.SampleLength / mem_size.value for w in self.Waveforms]  # Fraction of memory each Waveform needs
        assert sum(fracs) < 1, "Combined Waveforms are too large for memory!!!"

        ## Determines ideal scheme for splitting Board Memory ##
        segs_per_wave = np.ones(len(self.Waveforms), dtype=int32)   # Number of memory segments each Waveform requires
        num_segs = 2**ceil(log2(segs_per_wave.sum()))  # Minimum splitting required (must always be power of 2)

        searching = True  # Keeps segmenting the memory until each Waveform can be accommodated for
        while searching:
            searching = False
            for i, wav in enumerate(self.Waveforms):
                while fracs[i] > segs_per_wave[i] / num_segs:   # If a Waveform cannot fit in allocated segments,
                    segs_per_wave[i] += 1                   # give it another free segment.
                    if segs_per_wave.sum() > num_segs:   # If we run out of free segments
                        num_segs <<= 1                # halve each segment
                        searching = True          # and re-check each waveform (since segment size has changed).

        ## Splits the Board Memory ##
        spcm_dwSetParam_i32(self.hCard, SPC_SEQMODE_MAXSEGMENTS, num_segs)
        spcm_dwSetParam_i32(self.hCard, SPC_SEQMODE_STARTSTEP, 0)

        ## Sets up a local Software Buffer for Transfer to Board ##
        buf_size = NUMPY_MAX*2*num_chan.value     # PC buffer size
        pv_buf = pvAllocMemPageAligned(buf_size)  # Allocates space on PC
        pn_buf = cast(pv_buf, ptr16)              # Casts pointer into something usable

        ## Capacity of Each Segment in Bytes ##
        seg_cap = mem_size / num_segs

        ## Writes Each Segment Accordingly ##
        seg_idx = 0
        steps = []
        for num_segs, wav in zip(segs_per_wave, self.Waveforms):
            remain = wav.Sample_Length % seg_cap

            print("Transferring Seg %d of size %d bytes..." % (seg_idx, wav.Sample_Length*2))
            start = time()
            for i in range(num_segs):
                seg_size = remain if i == num_segs-1 else seg_cap
                spcm_dwSetParam_i32(self.hCard, SPC_SEQMODE_WRITESEGMENT, seg_idx)
                spcm_dwSetParam_i32(self.hCard, SPC_SEQMODE_SEGMENTSIZE,  int32(seg_size))
                self._error_check()

                so_far = 0
                for n in range(ceil(seg_size / NUMPY_MAX)):
                    seg_size_part = min(NUMPY_MAX, seg_size - n*NUMPY_MAX)
                    wav.load(pn_buf, so_far, seg_size_part)  # Fills the Buffer

                    ## Do a Transfer ##
                    spcm_dwDefTransfer_i64(self.hCard, SPCM_BUF_DATA, SPCM_DIR_PCTOCARD, 0, pv_buf,
                                           uint64(so_far), uint64(seg_size_part))
                    spcm_dwSetParam_i32(self.hCard, SPC_M2CMD, M2CMD_DATA_STARTDMA | M2CMD_DATA_WAITDMA)

                    so_far += seg_size_part   # Keep track of total transfer
                print("%d%c" % (int(100*(i+1)/num_segs), '%'))

                loops = 1 if (seg_idx % 2) == 1 else 10000  # Hardcoded stationary steps...
                next_seg = (seg_idx + 1) % sum(segs_per_wave)
                steps.append(Step(seg_idx, seg_idx, loops, next_seg))  # ...to patch up segmented single waveforms

                seg_idx += 1

            rate = wav.Sample_Length*2 // (time() - start)
            print("Average Transfer rate: %d bytes/second" % rate)
        self.load_sequence(steps, verbose)

    def _setup_clock(self, verbose):
        spcm_dwSetParam_i32(self.hCard, SPC_CLOCKMODE, SPC_CM_INTPLL)  # Sets out internal Quarts Clock For Sampling
        spcm_dwSetParam_i64(self.hCard, SPC_SAMPLERATE, int64(int(SAMP_FREQ)))  # Sets Sampling Rate
        spcm_dwSetParam_i32(self.hCard, SPC_CLOCKOUT, 0)  # Disables Clock Output
        check_clock = int64(0)
        spcm_dwGetParam_i64(self.hCard, SPC_SAMPLERATE, byref(check_clock))  # Checks Sampling Rate
        if verbose:
            print("Achieved Sampling Rate: ", check_clock.value)

    def _update_magnitudes(self, new_magnitudes):
        """ Turns off card, modifies each tone's magnitude, then lights it back up.
        """
        spcm_dwSetParam_i32(self.hCard, SPC_M2CMD, M2CMD_CARD_STOP)
        self.Waveforms[0].set_magnitudes(new_magnitudes)
        self.setup_buffer()
        spcm_dwSetParam_i32(self.hCard, SPC_M2CMD, M2CMD_CARD_START | M2CMD_CARD_ENABLETRIGGER)
        sleep(1)

    def _run_cam(self, which_cam, verbose=False):
        """ Fires up the camera stream (ThorLabs UC480)
        """
        ## https://instrumental-lib.readthedocs.io/en/stable/uc480-cameras.html ##
        ## ^^LOOK HERE^^ for driver documentation ##

        ## If you have problems here ##
        ## then see above doc &      ##
        ## Y:\E6\Software\Python\Instrument Control\ThorLabs UC480\cam_control.py ##

        names = ['ThorCam', 'ChamberCam']  # False, True
        cam = instrument(names[which_cam])

        ## Cam Live Stream ##
        cam.start_live_video(framerate=10 * u.hertz)

        ## Create Figure ##
        fig = plt.figure()
        ax1 = fig.add_subplot(1, 1, 1)

        ## Animation Frame ##
        def animate(i):
            if cam.wait_for_frame():
                im = cam.latest_frame()
                ax1.clear()
                if which_cam:
                    im = im[300:501, 300:501]
                ax1.imshow(im)

        ## Button: Automatic Exposure Adjustment ##
        def find_exposure(event):
            fix_exposure(cam, set_exposure, verbose)

        ## Button: Intensity Feedback ##
        def stabilize(event):  # Wrapper for Intensity Feedback function.
            self.stabilize_intensity(which_cam, cam, verbose)

        def snapshot(event):
            im = cam.latest_frame()
            plot_image(which_cam, im, 12, guess=True)

        def switch_cam(event):
            nonlocal cam, which_cam
            cam.close()

            which_cam = not which_cam

            cam = instrument(names[which_cam])
            cam.start_live_video(framerate=10 * u.hertz)

        ## Slider: Exposure ##
        def adjust_exposure(exp_t):
            cam._set_exposure(exp_t * u.milliseconds)

        ## Button Construction ##
        correct_exposure = Button(plt.axes([0.56, 0.0, 0.13, 0.05]), 'AutoExpose')
        stabilize_button = Button(plt.axes([0.7, 0.0, 0.1, 0.05]), 'Stabilize')
        plot_snapshot = Button(plt.axes([0.81, 0.0, 0.09, 0.05]), 'Plot')
        switch_cameras = Button(plt.axes([0.91, 0.0, 0.09, 0.05]), 'Switch')
        set_exposure = Slider(plt.axes([0.14, 0.9, 0.73, 0.05]), 'Exposure', 0.1, MAX_EXP, 20)

        correct_exposure.on_clicked(find_exposure)
        stabilize_button.on_clicked(stabilize)
        plot_snapshot.on_clicked(snapshot)
        switch_cameras.on_clicked(switch_cam)
        set_exposure.on_changed(adjust_exposure)

        ## Begin Animation ##
        _ = animation.FuncAnimation(fig, animate, interval=100)
        plt.show()
        cam.close()
        plt.close(fig)
        self._error_check()
