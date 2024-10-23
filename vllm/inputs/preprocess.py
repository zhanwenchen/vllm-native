import asyncio
from typing import List, Optional

from typing_extensions import assert_never

from vllm.config import ModelConfig
from vllm.logger import init_logger
from vllm.lora.request import LoRARequest
from vllm.prompt_adapter.request import PromptAdapterRequest
from vllm.transformers_utils.tokenizer_group import BaseTokenizerGroup
from vllm.utils import print_warning_once

from .data import (DecoderOnlyInputs, EncoderDecoderInputs, ProcessorInputs,
                   PromptType, SingletonPrompt, TokenInputs, token_inputs)
from .parse import is_explicit_encoder_decoder_prompt, parse_singleton_prompt

logger = init_logger(__name__)


class InputPreprocessor:

    def __init__(
        self,
        model_config: ModelConfig,
        tokenizer: Optional[BaseTokenizerGroup],
    ) -> None:
        super().__init__()

        self.model_config = model_config
        self.tokenizer = tokenizer

    def get_tokenizer_group(self) -> BaseTokenizerGroup:
        if self.tokenizer is None:
            raise ValueError("You cannot pass text prompts when "
                             "`skip_tokenizer_init` is True")

        return self.tokenizer

    def get_bos_token_id(self,
                         lora_request: Optional[LoRARequest] = None
                         ) -> Optional[int]:
        if self.tokenizer is None:
            logger.warning("Using None for BOS token id because tokenizer "
                           "is not initialized")
            return None

        return self.tokenizer.get_lora_tokenizer(lora_request).bos_token_id

    def get_eos_token_id(self,
                         lora_request: Optional[LoRARequest] = None
                         ) -> Optional[int]:
        if self.tokenizer is None:
            logger.warning("Using None for EOS token id because tokenizer "
                           "is not initialized")
            return None

        return self.tokenizer.get_lora_tokenizer(lora_request).eos_token_id

    def get_decoder_start_token_id(self) -> Optional[int]:
        '''
        Obtain the decoder start token id employed by an encoder/decoder
        model. Returns None for non-encoder/decoder models or if the
        model config is unavailable.
        '''

        if not self.is_encoder_decoder_model():
            print_warning_once("Using None for decoder start token id because "
                               "this is not an encoder/decoder model.")
            return None

        if (self.model_config is None or self.model_config.hf_config is None):
            print_warning_once("Using None for decoder start token id because "
                               "model config is not available.")
            return None

        dec_start_token_id = getattr(self.model_config.hf_config,
                                     'decoder_start_token_id', None)
        if dec_start_token_id is None:
            print_warning_once("Falling back on <BOS> for decoder start token "
                               "id because decoder start token id is not "
                               "available.")
            dec_start_token_id = self.get_bos_token_id()

        return dec_start_token_id

    def _get_default_enc_dec_decoder_prompt(self) -> List[int]:
        '''
        Specifically for encoder/decoder models:
        generate a default decoder prompt for when
        the user specifies only the encoder prompt.

        Encoder/decoder models utilize the decoder
        prompt in different ways; as new models are
        added, it is intended that this function
        will be extended to produce differing
        default decoder prompts, depending on the
        model variety.

        Absent a special case, the default behavior
        of this method is to mirror the behavior of
        the HuggingFace (HF) GenerationMixin for a None
        decoder prompt, which is to employ a logit processor
        setting to force the first decoded token to be <BOS>.
        Here, this behavior is approximated by having the
        "default" decoder prompt be <BOS>.

        However, it is possible that in the future
        other models may have different or more
        complex logic for the default decoder prompt.
        This motivates having a special helper method
        for default decoder prompts.

        Returns:

        * prompt_token_ids
        '''

        bos_token_id = self.get_bos_token_id()
        assert bos_token_id is not None
        return [bos_token_id]

    def _prepare_decoder_input_ids_for_generation(
        self,
        decoder_input_ids: Optional[List[int]],
        force_bos: bool = True,
    ) -> List[int]:
        """
        Prepares `decoder_input_ids` for generation with encoder-decoder models.

        Based on

        https://github.com/huggingface/transformers/blob/
        4037a2b5b1278736e566aec12e169100275545ea/
        src/transformers/generation/utils.py

        specifically GenerationMixin._prepare_decoder_input_ids_for_generation()

        Arguments:

        * decoder_input_ids: input token ids to preprocess

        Returns:

        * Processed token list
        """

        decoder_start_token_id = self.get_decoder_start_token_id()
        assert decoder_start_token_id is not None

        if decoder_input_ids is None:
            # no decoder prompt input ->
            # use decoder_start_token_id as decoder_input_ids
            decoder_input_ids = self._get_default_enc_dec_decoder_prompt()

        if force_bos and (len(decoder_input_ids) == 0
                          or decoder_input_ids[0] != decoder_start_token_id):
            decoder_input_ids = [decoder_start_token_id] + decoder_input_ids

        return decoder_input_ids

    def _apply_prompt_adapter(
        self,
        prompt_token_ids: List[int],
        prompt_adapter_request: Optional[PromptAdapterRequest],
    ) -> List[int]:
        if prompt_adapter_request:
            prompt_token_ids = (
                [0] * prompt_adapter_request.prompt_adapter_num_virtual_tokens
                + prompt_token_ids)

        return prompt_token_ids

    def _tokenize_prompt(
        self,
        prompt: str,
        request_id: str,
        lora_request: Optional[LoRARequest],
    ) -> List[int]:
        """
        Apply the model's tokenizer to a text prompt, returning the
        corresponding token IDs.
        """
        tokenizer = self.get_tokenizer_group()

        return tokenizer.encode(request_id=request_id,
                                prompt=prompt,
                                lora_request=lora_request)

    async def _tokenize_prompt_async(
        self,
        prompt: str,
        request_id: str,
        lora_request: Optional[LoRARequest],
    ) -> List[int]:
        """Async version of :meth:`_tokenize_prompt`."""
        tokenizer = self.get_tokenizer_group()

        return await tokenizer.encode_async(request_id=request_id,
                                            prompt=prompt,
                                            lora_request=lora_request)

    def _prompt_to_llm_inputs(
        self,
        prompt: SingletonPrompt,
        request_id: str,
        lora_request: Optional[LoRARequest] = None,
    ) -> DecoderOnlyInputs:
        '''
        Extract the components of any single encoder or decoder input prompt.

        Arguments:

        * request_id
        * prompt: single encoder or decoder input prompt
        * lora_request: this is only valid for decoder prompts

        Returns:

        * prompt
        * prompt_token_ids
        * multi_modal_data
        * mm_processor_kwargs (request-level input processor/mapper overrides)
        '''

        parsed = parse_singleton_prompt(prompt)

        if parsed["type"] == "str":
            prompt_text = parsed["content"]
            prompt_token_ids = self._tokenize_prompt(
                prompt_text,
                request_id=request_id,
                lora_request=lora_request,
            )

            return token_inputs(
                prompt=prompt_text,
                prompt_token_ids=prompt_token_ids,
            )

        if parsed["type"] == "tokens":
            prompt_token_ids = parsed["content"]["prompt_token_ids"]
            multi_modal_data = parsed["content"].get("multi_modal_data")
            mm_processor_kwargs = parsed["content"].get("mm_processor_kwargs")

            return token_inputs(
                prompt_token_ids=prompt_token_ids,
                multi_modal_data=multi_modal_data,
                mm_processor_kwargs=mm_processor_kwargs,
            )

        if parsed["type"] == "text":
            prompt_text = parsed["content"]["prompt"]
            prompt_token_ids = self._tokenize_prompt(
                prompt_text,
                request_id=request_id,
                lora_request=lora_request,
            )
            multi_modal_data = parsed["content"].get("multi_modal_data")
            mm_processor_kwargs = parsed["content"].get("mm_processor_kwargs")

            return token_inputs(
                prompt=prompt_text,
                prompt_token_ids=prompt_token_ids,
                multi_modal_data=multi_modal_data,
                mm_processor_kwargs=mm_processor_kwargs,
            )

        assert_never(parsed)

    async def _prompt_to_llm_inputs_async(
        self,
        prompt: SingletonPrompt,
        request_id: str,
        lora_request: Optional[LoRARequest] = None,
    ) -> DecoderOnlyInputs:
        """Async version of :meth:`_extract_prompt_components`."""
        parsed = parse_singleton_prompt(prompt)

        if parsed["type"] == "str":
            prompt_text = parsed["content"]
            prompt_token_ids = await self._tokenize_prompt_async(
                prompt_text,
                request_id=request_id,
                lora_request=lora_request,
            )

            return token_inputs(
                prompt=prompt_text,
                prompt_token_ids=prompt_token_ids,
            )

        if parsed["type"] == "tokens":
            prompt_token_ids = parsed["content"]["prompt_token_ids"]
            multi_modal_data = parsed["content"].get("multi_modal_data")
            mm_processor_kwargs = parsed["content"].get("mm_processor_kwargs")

            return token_inputs(
                prompt_token_ids=prompt_token_ids,
                multi_modal_data=multi_modal_data,
                mm_processor_kwargs=mm_processor_kwargs,
            )

        if parsed["type"] == "text":
            prompt_text = parsed["content"]["prompt"]
            prompt_token_ids = await self._tokenize_prompt_async(
                prompt_text,
                request_id=request_id,
                lora_request=lora_request,
            )
            multi_modal_data = parsed["content"].get("multi_modal_data")
            mm_processor_kwargs = parsed["content"].get("mm_processor_kwargs")

            return token_inputs(
                prompt=prompt_text,
                prompt_token_ids=prompt_token_ids,
                multi_modal_data=multi_modal_data,
                mm_processor_kwargs=mm_processor_kwargs,
            )

        assert_never(parsed)

    def _build_enc_dec_llm_inputs(
        self,
        encoder_inputs: TokenInputs,
        decoder_inputs: Optional[TokenInputs],
    ) -> EncoderDecoderInputs:
        if encoder_inputs["type"] == "token":
            pass
        else:
            assert_never(encoder_inputs)

        if decoder_inputs is None:
            dec_token_ids = self._prepare_decoder_input_ids_for_generation(
                None,
                force_bos="multi_modal_data" not in encoder_inputs,
            )
            decoder_inputs = token_inputs(dec_token_ids)
        elif decoder_inputs["type"] == "token":
            dec_token_ids = self._prepare_decoder_input_ids_for_generation(
                decoder_inputs["prompt_token_ids"],
                force_bos=("multi_modal_data" not in encoder_inputs
                           and "multi_modal_data" not in decoder_inputs),
            )
            decoder_inputs["prompt_token_ids"] = dec_token_ids

            if "multi_modal_data" in decoder_inputs:
                raise ValueError("Multi-modal decoder inputs of encoder-"
                                 "decoder models are not supported yet")
        else:
            assert_never(encoder_inputs)

        return EncoderDecoderInputs(
            encoder=encoder_inputs,
            decoder=decoder_inputs,
        )

    def _process_encoder_decoder_prompt(
        self,
        prompt: PromptType,
        request_id: str,
    ) -> EncoderDecoderInputs:
        '''
        For encoder/decoder models only:
        Process an input prompt into an :class:`EncoderDecoderInputs` instance.

        There are two types of input prompts:
        singleton prompts which carry only the
        encoder prompt, and explicit encoder/decoder
        prompts which carry both the encoder and the
        decoder prompts as member variables.

        This function handles the following scenarios:
        * Singleton encoder prompt: extract encoder prompt
          token ids & infer default decoder prompt token ids
        * Explicit encoder/decoder prompt: extract encoder
          and decoder prompt token ids

        Note that for Explicit encoder/decoder prompts,
        each sub-prompt (encoder or decoder prompt) can
        have any possible singleton type; thus this
        method relies on helper functions to obtain
        token ids for the sub-prompts.

        Arguments:

        * prompt: an input prompt
        * request_id

        Returns:

        * :class:`EncoderDecoderInputs` instance
        '''

        encoder_inputs: TokenInputs
        decoder_inputs: Optional[TokenInputs]

        if is_explicit_encoder_decoder_prompt(prompt):
            encoder_inputs = self._prompt_to_llm_inputs(
                prompt["encoder_prompt"],
                request_id=request_id,
            )

            if (decoder_input := prompt["decoder_prompt"]) is None:
                decoder_inputs = None
            else:
                decoder_inputs = self._prompt_to_llm_inputs(
                    decoder_input,
                    request_id=request_id,
                )
        else:
            encoder_inputs = self._prompt_to_llm_inputs(
                prompt,
                request_id=request_id,
            )

            decoder_inputs = None

        return self._build_enc_dec_llm_inputs(encoder_inputs, decoder_inputs)

    async def _process_encoder_decoder_prompt_async(
        self,
        prompt: PromptType,
        request_id: str,
    ) -> EncoderDecoderInputs:
        """Async version of :meth:`_process_encoder_decoder_prompt`."""
        encoder_inputs: TokenInputs
        decoder_inputs: Optional[TokenInputs]

        if is_explicit_encoder_decoder_prompt(prompt):
            encoder_task = self._prompt_to_llm_inputs_async(
                prompt["encoder_prompt"],
                request_id=request_id,
            )

            if (decoder_input := prompt["decoder_prompt"]) is None:
                encoder_inputs = await encoder_task
                decoder_inputs = None
            else:
                decoder_task = self._prompt_to_llm_inputs_async(
                    decoder_input,
                    request_id=request_id,
                )

                encoder_inputs, decoder_inputs = await asyncio.gather(
                    encoder_task, decoder_task)
        else:
            encoder_inputs = await self._prompt_to_llm_inputs_async(
                prompt,
                request_id=request_id,
            )

            decoder_inputs = None

        return self._build_enc_dec_llm_inputs(encoder_inputs, decoder_inputs)

    def _build_decoder_only_llm_inputs(
        self,
        prompt_inputs: DecoderOnlyInputs,
        prompt_adapter_request: Optional[PromptAdapterRequest],
    ) -> DecoderOnlyInputs:
        if prompt_inputs["type"] == "token":
            prompt_inputs["prompt_token_ids"] = self._apply_prompt_adapter(
                prompt_inputs["prompt_token_ids"],
                prompt_adapter_request=prompt_adapter_request,
            )
        else:
            assert_never(prompt_inputs)

        return prompt_inputs

    def _process_decoder_only_prompt(
        self,
        prompt: SingletonPrompt,
        request_id: str,
        lora_request: Optional[LoRARequest] = None,
        prompt_adapter_request: Optional[PromptAdapterRequest] = None,
    ) -> DecoderOnlyInputs:
        '''
        For decoder-only models:
        Process an input prompt into an :class:`DecoderOnlyInputs` instance.

        Arguments:

        * prompt: input prompt
        * request_id
        * lora_request
        * prompt_adapter_request

        Returns:

        * :class:`DecoderOnlyInputs` instance
        '''

        prompt_comps = self._prompt_to_llm_inputs(
            prompt,
            request_id=request_id,
            lora_request=lora_request,
        )

        return self._build_decoder_only_llm_inputs(
            prompt_comps,
            prompt_adapter_request=prompt_adapter_request,
        )

    async def _process_decoder_only_prompt_async(
        self,
        prompt: SingletonPrompt,
        request_id: str,
        lora_request: Optional[LoRARequest] = None,
        prompt_adapter_request: Optional[PromptAdapterRequest] = None,
    ) -> DecoderOnlyInputs:
        """Async version of :meth:`_process_decoder_only_prompt`."""
        prompt_comps = await self._prompt_to_llm_inputs_async(
            prompt,
            request_id=request_id,
            lora_request=lora_request,
        )

        return self._build_decoder_only_llm_inputs(
            prompt_comps,
            prompt_adapter_request=prompt_adapter_request,
        )

    def preprocess(
        self,
        prompt: PromptType,
        request_id: str,
        lora_request: Optional[LoRARequest] = None,
        prompt_adapter_request: Optional[PromptAdapterRequest] = None,
    ) -> ProcessorInputs:
        """Preprocess the input prompt."""
        if self.is_encoder_decoder_model():
            # Encoder-decoder model requires special mapping of
            # input prompts to encoder & decoder
            return self._process_encoder_decoder_prompt(
                prompt,
                request_id=request_id,
            )

        if is_explicit_encoder_decoder_prompt(prompt):
            raise ValueError("Cannot pass encoder-decoder prompt "
                             "to decoder-only models")

        # Decoder-only operation
        return self._process_decoder_only_prompt(
            prompt,
            request_id=request_id,
            lora_request=lora_request,
            prompt_adapter_request=prompt_adapter_request,
        )

    async def preprocess_async(
        self,
        prompt: PromptType,
        request_id: str,
        lora_request: Optional[LoRARequest] = None,
        prompt_adapter_request: Optional[PromptAdapterRequest] = None,
    ) -> ProcessorInputs:
        """Async version of :meth:`preprocess`."""
        if self.is_encoder_decoder_model():
            # Encoder-decoder model requires special mapping of
            # input prompts to encoder & decoder
            return await self._process_encoder_decoder_prompt_async(
                prompt,
                request_id=request_id,
            )

        if is_explicit_encoder_decoder_prompt(prompt):
            raise ValueError("Cannot pass encoder-decoder prompt "
                             "to decoder-only models")

        # Decoder-only operation
        return await self._process_decoder_only_prompt_async(
            prompt,
            request_id=request_id,
            lora_request=lora_request,
            prompt_adapter_request=prompt_adapter_request,
        )

    def is_encoder_decoder_model(self):
        return self.model_config.is_encoder_decoder_model
